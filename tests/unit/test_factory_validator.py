# pyrefly: ignore [missing-import]
"""测试主 agent 的 final-answer validator(闸门 2)。

语义:
- 计划还有 pending/in-progress 项时不打回,回复原样放行,不往回复里拼提醒
  (未完成项由 turn_stream 在回复后统一向用户展示,拼进回复会显示两遍并污染历史)。
- 本轮最近一次工具调用失败时打回一次,提示里允许模型如实汇报失败;第二次放行。
"""

from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.agent.factory import (
    _build_latest_failure_retry_message,
    _latest_failure_issues,
    build_main_agent,
)
from openhachimi_agent.tools.planning import TodoState, TodoTask


def _signal(*issues):
    return {"reason": "final verification failed", "issues": list(issues)}


def _latest_failure(tool="web_fetch", detail="HTTP 404"):
    return {"type": "latest_execution_not_successful", "tool_name": tool, "status": "failed", "detail": detail}


def _unfinished(*items):
    return {"type": "unfinished_todos", "items": list(items)}


def test_latest_failure_issues_picks_only_failures():
    signal = _signal(_unfinished({"id": 1, "status": "pending"}), _latest_failure())

    assert _latest_failure_issues(signal) == [_latest_failure()]
    assert _latest_failure_issues(None) == []
    assert _latest_failure_issues(_signal(_unfinished({"id": 1, "status": "pending"}))) == []


def test_retry_message_allows_honest_failure_report():
    message = _build_latest_failure_retry_message([_latest_failure()])

    assert "web_fetch" in message
    assert "HTTP 404" in message
    assert "如实说明失败原因" in message
    assert "不要给用户回复任何文字" not in message


@pytest.fixture
def validator(mock_config):
    (mock_config.roles_dir / "default.md").write_text("# default\n\n测试用角色。\n", encoding="utf-8")
    agent = build_main_agent(mock_config, "default")
    return agent._output_validators[0].function


def _ctx(session_state):
    return SimpleNamespace(deps=SimpleNamespace(session_state=session_state, session_store=None, session_id="s1"))


def _state_with_pending_plan():
    todo = TodoState(goal="g", is_active=True, tasks={1: TodoTask(id=1, description="写报告", status="pending")})
    return {"todo_state": todo}


def test_unfinished_todos_pass_through_unchanged(validator):
    state = _state_with_pending_plan()

    result = validator(_ctx(state), "已完成第一部分,报告还没写。")

    assert result == "已完成第一部分,报告还没写。"


def test_latest_failure_retries_once_then_passes(validator):
    state = {
        "execution_ledger": [{"seq": 1, "tool_name": "web_fetch", "status": "failed", "result_preview": "HTTP 404"}],
        "current_turn_ledger_start_seq": 0,
    }

    with pytest.raises(ModelRetry):
        validator(_ctx(state), "网页打不开。")

    # 第二次:模型如实汇报失败,应放行
    assert validator(_ctx(state), "这个网址返回 404,无法访问。") == "这个网址返回 404,无法访问。"
