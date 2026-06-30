from dataclasses import dataclass

import pytest
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.agent.execution import (
    get_execution_ledger,
    get_final_verification_signal,
    get_ledger_length,
    get_replan_signal,
    with_execution_ledger,
)
from openhachimi_agent.tools.planning import create_todos, update_todo


@dataclass
class MockRunContext:
    deps: object


def test_execution_ledger_records_success(mock_agent_deps):
    def read_file(ctx, path: str):
        return {"path": path, "content": "hello"}

    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(read_file)

    assert guarded(ctx, "README.md") == {"path": "README.md", "content": "hello"}
    ledger = get_execution_ledger(ctx)

    assert [event["status"] for event in ledger] == ["started", "succeeded"]
    assert ledger[0]["tool_name"] == "read_file"
    assert ledger[0]["args"]["path"] == "README.md"
    assert get_ledger_length(mock_agent_deps.session_state) == 2


def test_execution_ledger_records_tool_failure(mock_agent_deps):
    """工具抛异常时,ledger 记一条 started + 一条 failed。

    (旧版测的是 execution_guard 被 ledger 记成 blocked;guard 已随 Hermes 式
    重构拆除,改为测普通工具失败路径,确认 ledger 仍在记录失败。)
    """
    def write_file(ctx):
        raise RuntimeError("disk full")

    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(write_file)

    with pytest.raises(RuntimeError):
        guarded(ctx)

    ledger = get_execution_ledger(ctx)
    assert [event["status"] for event in ledger] == ["started", "failed"]
    assert ledger[-1]["tool_name"] == "write_file"




def test_replan_signal_requires_consecutive_failures(mock_agent_deps):
    """单次 blocked/failed 不触发 replan，连续 >=2 次才触发。"""
    # 单次 failed 后不触发 replan
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "browser_navigate", "status": "started"},
        {"seq": 2, "tool_name": "browser_navigate", "status": "failed", "result_preview": "timeout"},
    ]
    assert get_replan_signal(mock_agent_deps.session_state, since_seq=0) is None

    # 单次 failed 后跟 succeeded，不触发 replan
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "browser_navigate", "status": "blocked", "violation": "old"},
        {"seq": 2, "tool_name": "browser_navigate", "status": "started"},
        {"seq": 3, "tool_name": "browser_navigate", "status": "succeeded"},
    ]
    assert get_replan_signal(mock_agent_deps.session_state, since_seq=0) is None

    # 连续 2 次 failed，触发 replan
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "browser_navigate", "status": "started"},
        {"seq": 2, "tool_name": "browser_navigate", "status": "failed", "result_preview": "timeout"},
        {"seq": 3, "tool_name": "run_command", "status": "failed", "result_preview": "boom"},
    ]
    signal = get_replan_signal(mock_agent_deps.session_state, since_seq=0)
    assert signal is not None
    assert signal["consecutive_failures"] == 2
    assert signal["latest_status"] == "failed"


def test_final_verifier_detects_unfinished_todos(mock_agent_deps):
    ctx = MockRunContext(deps=mock_agent_deps)
    create_todos(ctx, ["Open the page"])

    signal = get_final_verification_signal(mock_agent_deps.session_state)

    assert signal is not None
    assert signal["issues"][0]["type"] == "unfinished_todos"

    update_todo(ctx, 1, "done", "opened")
    assert get_final_verification_signal(mock_agent_deps.session_state) is None


def test_final_verifier_treats_blocked_as_terminal(mock_agent_deps):
    """blocked 是模型诚实声明的合法终止态(缺资源/缺凭据/外部条件不满足),
    不应被当成"未完成证据"触发"[最终验证未通过]"提示。两个任务一个 done
    一个 blocked → 应返回 None。"""
    ctx = MockRunContext(deps=mock_agent_deps)
    create_todos(ctx, ["fetch", "send mail"])
    update_todo(ctx, 1, "done", "fetched")
    update_todo(ctx, 2, "blocked", "no smtp credentials available")

    assert get_final_verification_signal(mock_agent_deps.session_state) is None


def test_final_verifier_ignores_blocked_ledger_event(mock_agent_deps):
    """ledger 里 status=blocked 的事件来自 ExecutionGuardViolation(内部信号),
    不应被当成"最近一次工具失败"上报给用户。"""
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "write_file", "status": "blocked", "violation": "guard"}
    ]
    assert get_final_verification_signal(mock_agent_deps.session_state) is None


def test_final_verifier_detects_latest_failed_event(mock_agent_deps):
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "run_command", "status": "failed", "result_preview": "boom"}
    ]

    signal = get_final_verification_signal(mock_agent_deps.session_state)

    assert signal is not None
    assert signal["issues"][0]["type"] == "latest_execution_not_successful"


def test_final_verifier_ignores_suspended_todos(mock_agent_deps):
    ctx = MockRunContext(deps=mock_agent_deps)
    create_todos(ctx, ["Open the page"])
    mock_agent_deps.session_state["todo_state"].is_active = False

    assert get_final_verification_signal(mock_agent_deps.session_state) is None


def test_final_verifier_ignores_previous_turn_failed_event(mock_agent_deps):
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "run_command", "status": "failed", "result_preview": "old boom"},
        {"seq": 2, "tool_name": "read_file", "status": "succeeded", "result_preview": "ok"},
    ]
    mock_agent_deps.session_state["current_turn_ledger_start_seq"] = 1

    assert get_final_verification_signal(mock_agent_deps.session_state) is None
