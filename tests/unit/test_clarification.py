# pyrefly: ignore [missing-import]
import pytest
from dataclasses import dataclass
from unittest.mock import MagicMock

from openhachimi_agent.tools.clarification import clarify_user
from openhachimi_agent.tools.planning import create_todos, update_todo


@dataclass
class MockRunContext:
    deps: MagicMock


@pytest.fixture
def mock_ctx(mock_agent_deps):
    return MockRunContext(deps=mock_agent_deps)


def test_clarify_user_rejects_empty_question(mock_ctx):
    res = clarify_user(mock_ctx, "   ")
    assert res.startswith("错误")
    assert "_user_clarification" not in mock_ctx.deps.session_state


def test_clarify_user_sets_session_flag_when_no_plan(mock_ctx):
    """无活动计划时,只设 _user_clarification 标志(没有计划可挂起)。"""
    res = clarify_user(
        mock_ctx,
        question="请提供发件人邮箱与 SMTP 授权码",
        missing_inputs=["发件人邮箱", "SMTP 授权码"],
    )

    state = mock_ctx.deps.session_state
    flag = state.get("_user_clarification")
    assert isinstance(flag, dict)
    assert flag["question"] == "请提供发件人邮箱与 SMTP 授权码"
    assert flag["missing_inputs"] == ["发件人邮箱", "SMTP 授权码"]
    # 无 plan 时不会写 suspended_plan
    assert "suspended_plan" not in state
    assert "本轮将结束" in res


def test_clarify_user_suspends_active_plan(mock_ctx):
    """有活动计划时,应挂起计划并写 suspended_plan。"""
    create_todos(mock_ctx, ["task A", "task B"])
    update_todo(mock_ctx, 1, "in-progress")

    state_before = mock_ctx.deps.session_state["todo_state"]
    assert state_before.is_active is True

    clarify_user(mock_ctx, "需要凭据吗?", missing_inputs=["api_key"])

    state = mock_ctx.deps.session_state
    assert state.get("_user_clarification", {}).get("question") == "需要凭据吗?"

    # 计划应已挂起
    suspended = state.get("suspended_plan")
    assert isinstance(suspended, dict)
    assert suspended["reason"] == "awaiting_user_clarification"
    detail = suspended["detail"]
    assert detail["question"] == "需要凭据吗?"
    assert detail["missing_inputs"] == ["api_key"]

    # todo_state.is_active 翻成 False(挂起态)
    assert mock_ctx.deps.session_state["todo_state"].is_active is False


def test_clarify_user_second_call_in_same_turn_is_idempotent(mock_ctx):
    """同一轮内连续调用 clarify_user 应幂等:第一次正常工作,第二次起拒绝写入并
    要求模型转向输出文字。这是模型常见的"打磨措辞反复调"行为的兜底。
    """
    first = clarify_user(mock_ctx, "请提供 SMTP 凭据")
    assert "[已记录待澄清" in first

    state = mock_ctx.deps.session_state
    original = dict(state["_user_clarification"])

    # 第二次:打磨措辞稍变,session_state 不应被改写
    second = clarify_user(
        mock_ctx,
        "请提供 SMTP 服务器地址、端口、发件人邮箱和授权码",
    )

    assert "本轮已调用过" in second
    assert "不要" in second
    # session_state 仍是第一次的内容,不被打磨调用覆盖
    assert state["_user_clarification"] == original


def test_clarify_user_duplicate_does_not_resuspend(mock_ctx):
    """有活动计划时,第一次 clarify_user 挂起计划;第二次重复调用不应再次挂起
    或污染 suspended_plan(避免反复写同一字段、混淆下游 continuation 决策)。"""
    create_todos(mock_ctx, ["task A"])
    update_todo(mock_ctx, 1, "in-progress")
    clarify_user(mock_ctx, "Q1?")

    suspended_before = dict(mock_ctx.deps.session_state["suspended_plan"])

    clarify_user(mock_ctx, "Q1 (refined)?")

    # suspended_plan 内容应不变
    assert mock_ctx.deps.session_state["suspended_plan"] == suspended_before

