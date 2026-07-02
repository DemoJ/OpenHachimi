# pyrefly: ignore [missing-import]
"""测试 final-answer validator 的"未完成 TODO 软提醒"逻辑。

直接测 ``_build_unfinished_reminder`` helper 而非完整 closure,这样无需搭建完整
Agent + RunContext + OpenAI provider。closure 主体仅是:有 reminder 则追加到 result
末尾后放行(不打回),无 reminder 则继续走 latest_failure 打回路径,无独立可测逻辑。

语义(对齐 Hermes,2026-07 重构):
- unfinished_todos 不再触发 ModelRetry 打回,改为在最终回复末尾追加软提醒后放行。
- 仅 pending/in-progress 计入提醒;blocked/done 是合法终止态,不提醒。
- 若 signal 同时含 latest_execution_not_successful,返回 None(失败优先走打回,
  软提醒不重复出现)。
- 无 signal / 无未完成项 → 返回 None。
"""

from openhachimi_agent.agent.factory import _build_unfinished_reminder


def _signal(*issues):
    return {"reason": "final verification failed", "issues": list(issues)}


def _unfinished(*items):
    return {"type": "unfinished_todos", "items": list(items)}


def _latest_failure(tool="write_file", detail="disk full"):
    return {"type": "latest_execution_not_successful", "tool_name": tool,
            "status": "failed", "detail": detail}


def test_no_signal_returns_none():
    assert _build_unfinished_reminder(None) is None
    assert _build_unfinished_reminder({}) is None


def test_pending_items_produce_reminder():
    signal = _signal(_unfinished(
        {"id": 1, "description": "a", "status": "pending"},
        {"id": 2, "description": "b", "status": "in-progress"},
    ))
    reminder = _build_unfinished_reminder(signal)
    assert reminder is not None
    assert "2 项未完成" in reminder
    assert "[pending] #1 a" in reminder
    assert "[in-progress] #2 b" in reminder


def test_blocked_only_returns_none():
    """全 blocked 是合法终止态(模型诚实声明卡点),不需要软提醒。"""
    signal = _signal(_unfinished(
        {"id": 1, "description": "a", "status": "blocked"},
        {"id": 2, "description": "b", "status": "blocked"},
    ))
    assert _build_unfinished_reminder(signal) is None


def test_done_only_returns_none():
    """全 done 自然无需提醒。"""
    signal = _signal(_unfinished(
        {"id": 1, "description": "a", "status": "done"},
    ))
    assert _build_unfinished_reminder(signal) is None


def test_latest_failure_suppresses_reminder():
    """工具刚失败时,失败优先走打回路径,软提醒不重复出现。"""
    signal = _signal(
        _unfinished({"id": 1, "description": "a", "status": "pending"}),
        _latest_failure(),
    )
    assert _build_unfinished_reminder(signal) is None


def test_mixed_pending_and_blocked_only_reminds_pending():
    """blocked 项不计入提醒,只提醒 pending/in-progress。"""
    signal = _signal(_unfinished(
        {"id": 1, "description": "done-item", "status": "done"},
        {"id": 2, "description": "blocked-item", "status": "blocked"},
        {"id": 3, "description": "pending-item", "status": "pending"},
    ))
    reminder = _build_unfinished_reminder(signal)
    assert reminder is not None
    assert "1 项未完成" in reminder
    assert "pending-item" in reminder
    assert "blocked-item" not in reminder
    assert "done-item" not in reminder
