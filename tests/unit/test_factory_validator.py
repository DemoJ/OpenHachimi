# pyrefly: ignore [missing-import]
"""测试 final-answer validator 的快速放行逻辑。

直接测 ``should_pass_through_validation`` helper 而非完整 closure,这样无需
搭建完整的 Agent + RunContext + OpenAI provider。closure 本身仅是把 helper
返回值转成日志 + 设置 session_state 标志 + return result,无独立逻辑。

历史上还有一条 ``_user_clarification`` 放行路径,在 clarify_user 切到
``CallDeferred`` 之后该路径已被删除——抛 CallDeferred 让 run 在 graph 层
立刻终止,output 是 ``DeferredToolRequests`` 而非 ``str``,validator 整段都
不会被触发。
"""

from openhachimi_agent.agent.factory import should_pass_through_validation


def _signal(*issues):
    return {"reason": "final verification failed", "issues": list(issues)}


def test_no_signal_returns_none():
    assert should_pass_through_validation(None, {}) is None
    assert should_pass_through_validation({}, {}) is None


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
