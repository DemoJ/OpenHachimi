# pyrefly: ignore [missing-import]
import pytest
from dataclasses import dataclass

from pydantic_ai.exceptions import CallDeferred

from openhachimi_agent.tools.clarification import clarify_user
from openhachimi_agent.tools.planning import create_todos, update_todo


@dataclass
class MockRunContext:
    deps: object
    tool_call_id: str = "call_abc123"


@pytest.fixture
def mock_ctx(mock_agent_deps):
    return MockRunContext(deps=mock_agent_deps)


def test_clarify_user_rejects_empty_question(mock_ctx):
    """空 question 抛 ValueError,让 pydantic-ai 的工具校验把它转 ModelRetry,
    给模型同 run 内补一次合法调用的机会。"""
    with pytest.raises(ValueError, match="question 不能为空"):
        clarify_user(mock_ctx, "   ")
    assert "_user_clarification" not in mock_ctx.deps.session_state


def test_clarify_user_raises_call_deferred_when_no_plan(mock_ctx):
    """无活动计划时,clarify_user 抛 CallDeferred 阻断本轮 run,只设
    ``_user_clarification`` 标志(没有计划可挂起)。"""
    with pytest.raises(CallDeferred) as exc_info:
        clarify_user(
            mock_ctx,
            question="请提供发件人邮箱与 SMTP 授权码",
            missing_inputs=["发件人邮箱", "SMTP 授权码"],
        )

    state = mock_ctx.deps.session_state
    flag = state.get("_user_clarification")
    assert isinstance(flag, dict)
    assert flag["question"] == "请提供发件人邮箱与 SMTP 授权码"
    assert flag["missing_inputs"] == ["发件人邮箱", "SMTP 授权码"]
    assert flag["tool_call_id"] == "call_abc123"
    # 无 plan 时不会写 suspended_plan
    assert "suspended_plan" not in state

    # CallDeferred metadata 传到 pydantic-ai agent_graph,供 DeferredToolRequests.metadata 使用
    assert exc_info.value.metadata is not None
    assert exc_info.value.metadata["kind"] == "clarify_user"
    assert exc_info.value.metadata["question"] == "请提供发件人邮箱与 SMTP 授权码"
    assert exc_info.value.metadata["missing_inputs"] == ["发件人邮箱", "SMTP 授权码"]


def test_clarify_user_suspends_active_plan(mock_ctx):
    """有活动计划时,应同时挂起计划并抛 CallDeferred。"""
    create_todos(mock_ctx, ["task A", "task B"])
    update_todo(mock_ctx, 1, "in-progress")

    assert mock_ctx.deps.session_state["todo_state"].is_active is True

    with pytest.raises(CallDeferred):
        clarify_user(mock_ctx, "需要凭据吗?", missing_inputs=["api_key"])

    state = mock_ctx.deps.session_state
    assert state.get("_user_clarification", {}).get("question") == "需要凭据吗?"

    suspended = state.get("suspended_plan")
    assert isinstance(suspended, dict)
    assert suspended["reason"] == "awaiting_user_clarification"
    detail = suspended["detail"]
    assert detail["question"] == "需要凭据吗?"
    assert detail["missing_inputs"] == ["api_key"]

    # todo_state.is_active 翻成 False(挂起态)
    assert mock_ctx.deps.session_state["todo_state"].is_active is False


def test_clarify_user_accepts_json_string_missing_inputs(mock_ctx):
    """模型常把 list[str] 输出成 JSON 字符串 ``"[\"a\", \"b\"]"``;工具应该接得住,
    避免 pydantic-ai schema 校验失败 → ModelRetry → 模型反复重试同一调用。"""
    with pytest.raises(CallDeferred):
        clarify_user(
            mock_ctx,
            question="请给凭据",
            missing_inputs='["发件人邮箱", "SMTP 授权码"]',  # 字符串化 list,而非真 list
        )
    flag = mock_ctx.deps.session_state["_user_clarification"]
    assert flag["missing_inputs"] == ["发件人邮箱", "SMTP 授权码"]


def test_clarify_user_accepts_comma_separated_string(mock_ctx):
    """退化情形:模型把 missing_inputs 直接写成 ``"email, password"``。"""
    with pytest.raises(CallDeferred):
        clarify_user(mock_ctx, question="问", missing_inputs="email, password")
    assert mock_ctx.deps.session_state["_user_clarification"]["missing_inputs"] == [
        "email",
        "password",
    ]
