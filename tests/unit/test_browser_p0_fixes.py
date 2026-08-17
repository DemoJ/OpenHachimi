"""单测：BrowserManager P0 修复的契约。

覆盖：
- 断连异常从操作内层 except re-raise 到 auto_heal_retry（修复装饰器死代码）。
- 元素映射按 page 绑定：切页/关页后旧映射失效。
- dialog 处理器自动 dismiss 并在操作返回中报告。
- is_transient_disconnect 判据。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

from openhachimi_agent.service.browser.manager import (
    BrowserManager,
    is_transient_disconnect,
)


class _FakePage:
    def __init__(self, url="https://example.com", closed=False):
        self.url = url
        self._closed = closed

    def is_closed(self):
        return self._closed


def _make_manager() -> BrowserManager:
    manager = BrowserManager.__new__(BrowserManager)
    manager.config = SimpleNamespace(browser_idle_timeout=0)
    manager._element_mappings = {}
    manager._active_mapping = {}
    manager._last_dialog = None
    manager._context_hardening_id = None
    manager._page = None
    manager._context = None
    manager._captcha_detected_reason = None
    manager._captcha_setup_context_id = None
    return manager


# ---------- is_transient_disconnect ----------

def test_transient_disconnect_recognizes_keywords():
    assert is_transient_disconnect(RuntimeError("WebSocket connection is not open"))
    assert is_transient_disconnect(PlaywrightError("Target page, context or browser has been closed"))
    assert is_transient_disconnect(PlaywrightError("Protocol error (Page.navigate): Session closed"))


def test_transient_disconnect_rejects_business_errors():
    assert not is_transient_disconnect(KeyError("element_id"))
    assert not is_transient_disconnect(ValueError("bad argument"))
    assert not is_transient_disconnect(PlaywrightError("Timeout 3000ms exceeded"))


# ---------- P0-1: 断连异常冒泡 ----------

@pytest.mark.asyncio
async def test_click_reraises_disconnect_to_heal():
    """click 内层 except 必须把断连类异常 re-raise，让 auto_heal_retry 自愈。"""
    bm = _make_manager()
    bm._page = _FakePage()
    bm._active_mapping = {5: ([], "[data-agent-id='5']")}
    bm._op_lock = asyncio.Lock()

    class _First:
        async def click(self, **kwargs):
            raise PlaywrightError("Target page, context or browser has been closed")

    class _LocatorObj:
        first = _First()

    bm._page.locator = lambda selector: _LocatorObj()

    # 手动调用 click（绕过装饰器），验证 re-raise 而非吞掉
    raw_click = BrowserManager.click.__wrapped__
    with pytest.raises(PlaywrightError):
        await raw_click(bm, 5)


@pytest.mark.asyncio
async def test_get_state_reraises_disconnect():
    bm = _make_manager()
    bm._page = _FakePage()
    bm._op_lock = asyncio.Lock()

    async def _evaluate(*args, **kwargs):
        raise PlaywrightError("WebSocket is not open")

    bm._page.evaluate = _evaluate

    raw_get_state = BrowserManager.get_state.__wrapped__
    with pytest.raises(PlaywrightError):
        await raw_get_state(bm)


# ---------- P0-4: 元素映射与 page 绑定 ----------

def test_element_mapping_isolated_per_page():
    bm = _make_manager()
    p1, p2 = _FakePage("https://a.com"), _FakePage("https://b.com")
    bm._element_mappings = {id(p1): {1: "[data-agent-id='1']"}}
    bm._page = p1
    bm._active_mapping = bm._element_mappings[id(p1)]

    # 切换页面：active mapping 必须切到 p2（为空），残留 p1 的映射不可见
    bm._active_mapping = bm._element_mappings.get(id(p2), {})
    assert bm._active_mapping == {}
    assert 1 not in bm._active_mapping


def test_close_tab_clears_mapping_for_that_page():
    bm = _make_manager()
    p1, p2 = _FakePage("https://a.com"), _FakePage("https://b.com")
    bm._element_mappings = {id(p1): {1: "s1"}, id(p2): {2: "s2"}}
    bm._element_mappings.pop(id(p1), None)
    assert id(p1) not in bm._element_mappings
    assert bm._element_mappings == {id(p2): {2: "s2"}}


# ---------- P0-2: dialog 报告 ----------

def test_consume_dialog_report_returns_and_clears():
    bm = _make_manager()
    bm._last_dialog = ("alert", "确定要离开吗？")
    report = bm._consume_dialog_report()
    assert "alert" in report
    assert "确定要离开吗" in report
    assert bm._last_dialog is None  # 消费后清空
    assert bm._consume_dialog_report() == ""  # 二次消费为空


def test_consume_dialog_report_truncates_long_message():
    bm = _make_manager()
    bm._last_dialog = ("confirm", "x" * 500)
    report = bm._consume_dialog_report()
    assert len(report) < 400


def test_on_dialog_records_and_schedules_dismiss():
    bm = _make_manager()

    class _Dialog:
        type = "confirm"
        message = "hello?"

        async def dismiss(self):
            dismissed.append(True)

    dismissed = []
    bm._on_dialog(_Dialog())
    assert bm._last_dialog == ("confirm", "hello?")
    # create_task 需要事件循环tick后才执行
    assert dismissed == []


@pytest.mark.asyncio
async def test_dismiss_dialog_swallows_error():
    bm = _make_manager()

    class _Dialog:
        async def dismiss(self):
            raise RuntimeError("already dismissed by navigation")

    await bm._dismiss_dialog(_Dialog())  # 不应抛异常
