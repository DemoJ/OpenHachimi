"""verification_stop 停止闸门单测。"""

from openhachimi_agent.agent.verification_evidence import is_edit_tool, is_verify_tool
from openhachimi_agent.agent.verification_stop import (
    build_verify_on_stop_nudge,
    mark_tool_succeeded,
    reset_turn_verification,
)


def _state(session_state):
    return session_state["_verification_state"]


def test_no_edit_no_nudge():
    """没编辑过工作区 → 闸门直接放行(None)。"""
    session_state = {}
    reset_turn_verification(session_state)
    assert build_verify_on_stop_nudge(session_state) is None


def test_edit_without_verify_nudges():
    """write_file 成功后无验证 → 闸门返回 nudge 文本。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file")
    nudge = build_verify_on_stop_nudge(session_state)
    assert nudge is not None
    assert "验证缺口" in nudge
    assert "write_file" in nudge
    assert _state(session_state)["nudge_attempts"] == 1


def test_edit_then_verify_clears_nudge():
    """write_file 后再 run_command/git_diff 验证 → 清 stale,闸门放行。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file")
    mark_tool_succeeded(session_state, "run_command")
    assert build_verify_on_stop_nudge(session_state) is None

    # read_file 核对也算验证
    mark_tool_succeeded(session_state, "write_file")
    mark_tool_succeeded(session_state, "read_file")
    assert build_verify_on_stop_nudge(session_state) is None


def test_run_command_self_verifies():
    """run_command 身兼 edit/verify 两职:成功后 net 不 stale(模型自查过)。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "run_command")
    assert build_verify_on_stop_nudge(session_state) is None


def test_max_attempts_then_release():
    """nudge 两次后放行,避免死循环。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file")
    assert build_verify_on_stop_nudge(session_state) is not None  # 第 1 次
    assert build_verify_on_stop_nudge(session_state) is not None  # 第 2 次
    assert _state(session_state)["nudge_attempts"] == 2
    assert build_verify_on_stop_nudge(session_state) is None  # 第 3 次:放行


def test_non_edit_tool_does_not_mark_stale():
    """非 edit/verify 工具(如 get_todos/update_todo)不改 stale 状态。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "get_todos")
    assert build_verify_on_stop_nudge(session_state) is None
    assert _state(session_state).get("workspace_edited") is not True


def test_evidence_classification():
    """工具分类谓词:edit/verify 集合正确。"""
    assert is_edit_tool("write_file")
    assert is_edit_tool("run_command")
    assert is_edit_tool("browser_click")
    assert not is_edit_tool("read_file")
    assert not is_edit_tool("get_todos")
    assert not is_edit_tool(None)

    assert is_verify_tool("run_command")
    assert is_verify_tool("git_diff")
    assert is_verify_tool("read_file")
    assert not is_verify_tool("write_file")
    assert not is_verify_tool(None)


def test_reset_clears_stale():
    """reset_turn_verification 清掉上一轮的 stale 状态。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file")
    assert _state(session_state)["workspace_edited"] is True
    reset_turn_verification(session_state)
    assert _state(session_state)["workspace_edited"] is False
    assert _state(session_state)["nudge_attempts"] == 0
