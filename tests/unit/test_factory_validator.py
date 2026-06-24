# pyrefly: ignore [missing-import]
"""测试 final-answer validator 的快速放行逻辑。

直接测 ``should_pass_through_validation`` helper 而非完整 closure,这样无需
搭建完整的 Agent + RunContext + OpenAI provider。closure 本身仅是把 helper
返回值转成日志 + 设置 session_state 标志 + return result,无独立逻辑。
"""

from openhachimi_agent.agent.factory import should_pass_through_validation


def _signal(*issues):
    return {"reason": "final verification failed", "issues": list(issues)}


def test_no_signal_returns_none():
    assert should_pass_through_validation(None, {}) is None
    assert should_pass_through_validation({}, {}) is None


def test_pass_through_when_clarify_user_invoked():
    """clarify_user 标志存在 → 放行,无论 signal 内容如何。"""
    signal = _signal({"type": "unfinished_todos", "items": [
        {"id": 1, "description": "x", "status": "pending"},
    ]})
    session_state = {"_user_clarification": {"question": "?", "missing_inputs": []}}
    reason = should_pass_through_validation(signal, session_state)
    assert reason is not None
    assert "clarify_user" in reason


def test_pass_through_when_all_unfinished_blocked():
    signal = _signal({"type": "unfinished_todos", "items": [
        {"id": 1, "description": "a", "status": "blocked"},
        {"id": 2, "description": "b", "status": "blocked"},
    ]})
    reason = should_pass_through_validation(signal, {})
    assert reason is not None
    assert "blocked" in reason
    assert "count=2" in reason


def test_still_blocks_when_pending_remains():
    """有 pending(未声明卡点)的任务时不放行,validator 应继续打回。"""
    signal = _signal({"type": "unfinished_todos", "items": [
        {"id": 1, "description": "a", "status": "blocked"},
        {"id": 2, "description": "b", "status": "pending"},
    ]})
    assert should_pass_through_validation(signal, {}) is None


def test_still_blocks_when_recent_tool_failed():
    """即便全 blocked,只要最近一次工具失败就不放行(模型可能在错误地标 blocked)。"""
    signal = _signal(
        {"type": "unfinished_todos", "items": [
            {"id": 1, "description": "a", "status": "blocked"},
        ]},
        {"type": "latest_execution_not_successful", "tool_name": "write_file",
         "status": "failed", "detail": "disk full"},
    )
    assert should_pass_through_validation(signal, {}) is None


def test_still_blocks_when_in_progress_remains():
    """status=in-progress 的任务也算"尚未诚实声明卡点",不放行。"""
    signal = _signal({"type": "unfinished_todos", "items": [
        {"id": 1, "description": "a", "status": "in-progress"},
    ]})
    assert should_pass_through_validation(signal, {}) is None
