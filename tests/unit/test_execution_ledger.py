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


@pytest.mark.asyncio
async def test_execution_ledger_blocks_narrow_target_substitution(mock_agent_deps):
    async def browser_navigate(ctx, url: str):
        return f"opened {url}"

    mock_agent_deps.session_state["task_frame"] = {
        "allowed_autonomy": "narrow",
        "target_entities": [
            {"type": "url", "value": "https://example.com/a", "role": "primary", "immutable": True}
        ],
        "invariants": ["Do not replace target URL https://example.com/a"],
    }
    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(browser_navigate)

    with pytest.raises(Exception, match="different URL before observing"):
        await guarded(ctx, "https://example.com/b")

    ledger = get_execution_ledger(ctx)
    assert ledger[-1]["status"] == "blocked"
    assert "https://example.com/b" in ledger[-1]["violation"]

    signal = get_replan_signal(mock_agent_deps.session_state, since_seq=0)
    assert signal is not None
    assert signal["latest_status"] == "blocked"
    assert signal["events"][-1]["tool_name"] == "browser_navigate"


@pytest.mark.asyncio
async def test_execution_ledger_allows_navigation_after_target_observed(mock_agent_deps):
    async def browser_navigate(ctx, url: str):
        return f"opened {url}"

    mock_agent_deps.session_state["task_frame"] = {
        "allowed_autonomy": "narrow",
        "target_entities": [
            {"type": "url", "value": "https://example.com/a", "role": "primary", "immutable": True}
        ],
        "invariants": ["Do not replace target URL https://example.com/a"],
    }
    ctx = MockRunContext(deps=mock_agent_deps)
    guarded = with_execution_ledger(browser_navigate)

    assert await guarded(ctx, "https://example.com/a/") == "opened https://example.com/a/"
    assert await guarded(ctx, "https://example.com/b") == "opened https://example.com/b"

    ledger = get_execution_ledger(ctx)
    assert [event["status"] for event in ledger] == ["started", "succeeded", "started", "succeeded"]
    assert mock_agent_deps.session_state["task_frame_observations"]["target_urls_observed"] == ["https://example.com/a"]
    assert get_replan_signal(mock_agent_deps.session_state, since_seq=0) is None


def test_replan_signal_only_considers_new_events(mock_agent_deps):
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "browser_navigate", "status": "blocked", "violation": "old"},
        {"seq": 2, "tool_name": "browser_navigate", "status": "started"},
        {"seq": 3, "tool_name": "browser_navigate", "status": "succeeded"},
    ]

    assert get_replan_signal(mock_agent_deps.session_state, since_seq=1) is None

    mock_agent_deps.session_state["execution_ledger"].append(
        {"seq": 4, "tool_name": "run_command", "status": "failed", "result_preview": "boom"}
    )

    signal = get_replan_signal(mock_agent_deps.session_state, since_seq=3)
    assert signal is not None
    assert signal["latest_status"] == "failed"
    assert signal["events"][-1]["detail"] == "boom"


def test_final_verifier_detects_unfinished_todos(mock_agent_deps):
    ctx = MockRunContext(deps=mock_agent_deps)
    create_todos(ctx, ["Open the page"])

    signal = get_final_verification_signal(mock_agent_deps.session_state)

    assert signal is not None
    assert signal["issues"][0]["type"] == "unfinished_todos"

    update_todo(ctx, 1, "done", "opened")
    assert get_final_verification_signal(mock_agent_deps.session_state) is None


def test_final_verifier_detects_missing_narrow_target_observation(mock_agent_deps):
    mock_agent_deps.session_state["task_frame"] = {
        "allowed_autonomy": "narrow",
        "target_entities": [
            {"type": "url", "value": "https://example.com/a", "role": "primary", "immutable": True}
        ],
    }

    signal = get_final_verification_signal(mock_agent_deps.session_state)

    assert signal is not None
    assert signal["issues"][0]["type"] == "target_urls_not_observed"


def test_final_verifier_detects_latest_failed_event(mock_agent_deps):
    mock_agent_deps.session_state["execution_ledger"] = [
        {"seq": 1, "tool_name": "run_command", "status": "failed", "result_preview": "boom"}
    ]

    signal = get_final_verification_signal(mock_agent_deps.session_state)

    assert signal is not None
    assert signal["issues"][0]["type"] == "latest_execution_not_successful"
