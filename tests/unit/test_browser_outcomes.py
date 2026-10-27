"""Business outcomes using fake browser objects only; no browser or network calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import asyncio

import pytest

from openhachimi_agent.agent.execution import (
    ToolOutcome,
    ToolResult,
    get_execution_ledger,
    get_tool_outcome,
    with_execution_ledger,
)
from openhachimi_agent.agent.verification_stop import build_verify_on_stop_nudge, mark_tool_succeeded
from openhachimi_agent.service.browser.interactions import BrowserInteractionsMixin
from openhachimi_agent.service.browser.manager import BrowserManager
from openhachimi_agent.service.browser.retry import auto_heal_retry, browser_failure
from openhachimi_agent.service.browser.tabs import BrowserTabsMixin
from openhachimi_agent.tools.browser import browser_click, browser_get_state, browser_navigate, browser_type


@pytest.fixture
def fake_browser(monkeypatch):
    # Avoid human interaction delays in these mocked unit tests.
    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr("openhachimi_agent.service.browser.interactions.asyncio.sleep", no_sleep)
    manager = MagicMock(spec=BrowserManager)
    manager._op_lock = asyncio.Lock()
    manager._page = MagicMock()
    manager._page.is_closed.return_value = False
    manager._page.url = "https://example.invalid/form"
    manager._active_mapping = {1: ([], "#submit")}
    manager._session_route.return_value = lambda: None
    manager._verify_element_description = AsyncMock(return_value=None)
    manager._human_move_click = AsyncMock(return_value=True)
    manager._page_snapshot = AsyncMock(return_value={})
    manager._diff_snapshot.return_value = ""
    manager._wait_dom_quiesce = AsyncMock(return_value=True)
    manager._update_active_page = AsyncMock()
    manager._detect_human_verification = AsyncMock(return_value=None)
    manager._get_valid_pages.return_value = [manager._page]
    manager._consume_dialog_report.return_value = ""
    return manager


@pytest.mark.asyncio
async def test_real_click_effect_requires_browser_observation(mock_agent_deps, fake_browser):
    fake_browser.click = BrowserInteractionsMixin.click.__get__(fake_browser)
    mock_agent_deps.browser_manager = fake_browser
    ctx = SimpleNamespace(deps=mock_agent_deps)

    result = await with_execution_ledger(browser_click)(ctx, 1)
    assert "成功点击" in result
    assert get_tool_outcome(result).effects == frozenset({"browser"})
    fake_browser._human_move_click.assert_awaited_once()
    state = mock_agent_deps.session_state["_verification_state"]
    assert not state.get("workspace_edited")
    assert state["browser_edited"]
    mark_tool_succeeded(mock_agent_deps.session_state, "git_status")
    assert build_verify_on_stop_nudge(mock_agent_deps.session_state) is not None
    mark_tool_succeeded(mock_agent_deps.session_state, "browser_get_state")
    assert build_verify_on_stop_nudge(mock_agent_deps.session_state) is None


@pytest.mark.asyncio
async def test_post_click_failure_keeps_partial_effect(mock_agent_deps, fake_browser):
    fake_browser.click = BrowserInteractionsMixin.click.__get__(fake_browser)
    fake_browser._update_active_page.side_effect = RuntimeError("post-click observation failed")
    mock_agent_deps.browser_manager = fake_browser
    ctx = SimpleNamespace(deps=mock_agent_deps)

    result = await with_execution_ledger(browser_click)(ctx, 1)
    assert get_tool_outcome(result) == ToolOutcome("failed", effects=frozenset({"browser"}))
    assert [event["status"] for event in get_execution_ledger(ctx)] == ["started", "failed"]
    assert get_execution_ledger(ctx)[-1]["effects"] == ["browser"]
    assert mock_agent_deps.session_state["_verification_state"]["browser_edited"]
    assert not mock_agent_deps.session_state["_verification_state"].get("workspace_edited")


@pytest.mark.asyncio
async def test_post_click_disconnect_does_not_double_submit(mock_agent_deps, fake_browser):
    fake_browser.click = BrowserInteractionsMixin.click.__get__(fake_browser)
    fake_browser._update_active_page.side_effect = RuntimeError("connection is not open")
    mock_agent_deps.browser_manager = fake_browser
    ctx = SimpleNamespace(deps=mock_agent_deps)

    result = await with_execution_ledger(browser_click)(ctx, 1)
    fake_browser._human_move_click.assert_awaited_once()
    assert get_tool_outcome(result) == ToolOutcome("failed", effects=frozenset({"browser"}))
    assert "未自动重试" in result
    assert get_execution_ledger(ctx)[-1]["status"] == "failed"
    assert mock_agent_deps.session_state["_verification_state"]["browser_edited"]


@pytest.mark.asyncio
async def test_click_guard_failure_has_no_effect(mock_agent_deps, fake_browser):
    fake_browser.click = BrowserInteractionsMixin.click.__get__(fake_browser)
    fake_browser._active_mapping = {}
    mock_agent_deps.browser_manager = fake_browser
    ctx = SimpleNamespace(deps=mock_agent_deps)

    result = await with_execution_ledger(browser_click)(ctx, 999)
    assert get_tool_outcome(result) == ToolOutcome("failed")
    fake_browser._human_move_click.assert_not_awaited()
    assert not mock_agent_deps.session_state["_verification_state"].get("browser_edited")
    assert build_verify_on_stop_nudge(mock_agent_deps.session_state) is None


@pytest.mark.asyncio
async def test_multistep_typing_failure_preserves_completed_input_effect(mock_agent_deps, fake_browser):
    fake_browser.type_text = BrowserInteractionsMixin.type_text.__get__(fake_browser)
    locator = SimpleNamespace(
        click=AsyncMock(), clear=AsyncMock(),
        press_sequentially=AsyncMock(side_effect=RuntimeError("typing interrupted")),
    )
    fake_browser._resolve_locator = AsyncMock(return_value=locator)
    mock_agent_deps.browser_manager = fake_browser
    ctx = SimpleNamespace(deps=mock_agent_deps)

    result = await with_execution_ledger(browser_type)(ctx, 1, "new value", simulate_typing=True)
    locator.clear.assert_awaited_once()
    assert get_tool_outcome(result) == ToolOutcome("failed", effects=frozenset({"browser"}))
    assert build_verify_on_stop_nudge(mock_agent_deps.session_state) is not None


@pytest.mark.asyncio
async def test_navigation_inner_failure_not_marked_success(mock_agent_deps, fake_browser):
    fake_browser.navigate = BrowserTabsMixin.navigate.__get__(fake_browser)
    fake_browser._normalize_url.side_effect = lambda url: url
    fake_browser._ensure_browser = AsyncMock(return_value=fake_browser._page)
    fake_browser._page.goto = AsyncMock(side_effect=RuntimeError("navigation failed"))
    mock_agent_deps.browser_manager = fake_browser
    ctx = SimpleNamespace(deps=mock_agent_deps)

    result = await with_execution_ledger(browser_navigate)(ctx, "https://example.invalid/other")
    assert get_tool_outcome(result) == ToolOutcome("failed")
    assert get_execution_ledger(ctx)[-1]["status"] == "failed"
    assert build_verify_on_stop_nudge(mock_agent_deps.session_state) is None


@pytest.mark.asyncio
async def test_failed_page_observation_cannot_verify_browser_mutation(mock_agent_deps, fake_browser):
    fake_browser.get_state = BrowserManager.get_state.__get__(fake_browser)
    fake_browser._page.evaluate = AsyncMock(side_effect=RuntimeError("snapshot failed"))
    mock_agent_deps.browser_manager = fake_browser
    ctx = SimpleNamespace(deps=mock_agent_deps)
    mark_tool_succeeded(mock_agent_deps.session_state, "browser_click")

    result = await with_execution_ledger(browser_get_state)(ctx)
    assert get_tool_outcome(result) == ToolOutcome("failed")
    assert mock_agent_deps.session_state["_verification_state"]["browser_edited"]
    assert get_execution_ledger(ctx)[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_retry_does_not_replay_reported_partial_effect(fake_browser):
    calls = 0
    fake_browser._ensure_browser = AsyncMock()

    @auto_heal_retry(max_retries=2, base_delay=0)
    async def operation(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            return browser_failure(
                "lost after action", error=RuntimeError("connection is not open"), changed=True,
            )
        return browser_failure("retry rejected before action")

    result = await operation(fake_browser)
    assert calls == 1
    fake_browser._ensure_browser.assert_not_awaited()
    assert get_tool_outcome(result) == ToolOutcome("failed", effects=frozenset({"browser"}))


@pytest.mark.asyncio
async def test_text_wait_is_scoped_evidence_but_sleep_is_not(fake_browser):
    fake_browser._page.evaluate = AsyncMock(return_value=True)
    result = await BrowserInteractionsMixin.wait_for(fake_browser, text="Saved")
    assert get_tool_outcome(result).evidence == frozenset({"browser"})

    result = await BrowserInteractionsMixin.wait_for(fake_browser, seconds=0.1)
    assert get_tool_outcome(result).evidence == frozenset()
    fake_browser._page.evaluate = AsyncMock(return_value=False)
    result = await BrowserInteractionsMixin.wait_for(fake_browser, text="Saved")
    assert get_tool_outcome(result) == ToolOutcome("failed")
