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


# ── 文件类型过滤:纯文本/文档编辑不触发验证 nudge ──


def test_non_code_edit_does_not_mark_stale():
    """write_file 改 .md/.txt 等纯文本 → 不置 stale,闸门放行(照搬 Hermes)。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", {"file_path": "docs/README.md"})
    assert _state(session_state).get("workspace_edited") is not True
    assert build_verify_on_stop_nudge(session_state) is None

    # .txt / LICENSE 同理
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", {"file_path": "NOTES.txt"})
    assert _state(session_state).get("workspace_edited") is not True

    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", {"file_path": "LICENSE"})
    assert _state(session_state).get("workspace_edited") is not True


def test_code_edit_still_marks_stale():
    """write_file 改 .py/.ts 等代码 → 仍置 stale,闸门 nudge。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", {"file_path": "src/app.py"})
    assert _state(session_state)["workspace_edited"] is True
    assert build_verify_on_stop_nudge(session_state) is not None


def test_replace_in_file_non_code_does_not_mark_stale():
    """replace_in_file 改 .md → 不置 stale。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "replace_in_file", {"file_path": "guide.md"})
    assert _state(session_state).get("workspace_edited") is not True


def test_edit_without_path_arg_conservatively_marks_stale():
    """读不到路径参数时保守置 stale(不误放,宁可多 nudge 一次)。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", None)
    assert _state(session_state)["workspace_edited"] is True


def test_non_file_edit_tool_ignores_path_arg():
    """run_command 身兼 edit/verify 两职:不论带什么路径参数,net 都自验证不 stale
    (文件类型过滤只对 write_file/replace_in_file 生效)。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "run_command", {"file_path": "readme.md"})
    assert build_verify_on_stop_nudge(session_state) is None


# ── nudge 引用最近验证证据 ──


class _FakeEvidenceStore:
    """最小 mock:load_recent_verification_evidence 返回预设列表。"""

    def __init__(self, evidence):
        self._evidence = evidence
        self.calls = []

    def load_recent_verification_evidence(self, session_id, limit=5):
        self.calls.append((session_id, limit))
        return self._evidence


def test_nudge_includes_recent_evidence_when_store_provided():
    """传 session_store + session_id 时,nudge 附上"上次验证"引用。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", {"file_path": "src/app.py"})
    store = _FakeEvidenceStore([{
        "command": "pytest -x",
        "cwd": ".",
        "output_summary": "3 failed, 1 passed",
        "is_running": False,
        "created_at": "2026-07-01T00:00:00Z",
        "seq": 1,
    }])
    nudge = build_verify_on_stop_nudge(session_state, session_store=store, session_id="sess-1")
    assert nudge is not None
    assert "上次验证参考" in nudge
    assert "pytest -x" in nudge
    assert "已结束" in nudge
    assert "3 failed, 1 passed" in nudge
    # 调用方传了正确的 session_id
    assert store.calls[-1][0] == "sess-1"


def test_nudge_without_store_degrades_to_original_text():
    """不传 session_store 时退化为原文案(向后兼容),不含"上次验证"引用。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", {"file_path": "src/app.py"})
    nudge = build_verify_on_stop_nudge(session_state)
    assert nudge is not None
    assert "上次验证参考" not in nudge
    assert "验证缺口" in nudge


def test_nudge_without_evidence_omits_reference():
    """传了 store 但无证据记录时,不附"上次验证"引用(退化为原文案)。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", {"file_path": "src/app.py"})
    store = _FakeEvidenceStore([])
    nudge = build_verify_on_stop_nudge(session_state, session_store=store, session_id="sess-1")
    assert nudge is not None
    assert "上次验证参考" not in nudge


def test_nudge_evidence_running_status_label():
    """证据 is_running=True 时显示"仍在运行中"。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", {"file_path": "src/app.py"})
    store = _FakeEvidenceStore([{
        "command": "pytest",
        "cwd": ".",
        "output_summary": "running...",
        "is_running": True,
        "created_at": "2026-07-01T00:00:00Z",
        "seq": 1,
    }])
    nudge = build_verify_on_stop_nudge(session_state, session_store=store, session_id="sess-1")
    assert nudge is not None
    assert "仍在运行中" in nudge


def test_nudge_evidence_summary_truncated_to_tail():
    """证据 output_summary 超长时只保留末尾 200 字。"""
    session_state = {}
    reset_turn_verification(session_state)
    mark_tool_succeeded(session_state, "write_file", {"file_path": "src/app.py"})
    long_summary = "HEAD-" + "y" * 300 + "-TAIL"
    store = _FakeEvidenceStore([{
        "command": "pytest",
        "cwd": ".",
        "output_summary": long_summary,
        "is_running": False,
        "created_at": "2026-07-01T00:00:00Z",
        "seq": 1,
    }])
    nudge = build_verify_on_stop_nudge(session_state, session_store=store, session_id="sess-1")
    assert nudge is not None
    # 末尾 TAIL 保留,头部 HEAD 被截断
    assert "TAIL" in nudge
    assert "HEAD-" not in nudge
