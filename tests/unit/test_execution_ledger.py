from dataclasses import dataclass

import pytest

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
