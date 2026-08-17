"""单测：CAPTCHA 分级检测契约（误报修复）。

覆盖：
- soft 级（小/隐藏 widget）不触发人工接管
- hard 级（大面积可见挑战框）触发人工接管
- 缓存复核：observer 误报缓存被主动检测清除（防粘住）
- soft 提示记录到 _captcha_soft_note 供 get_state 附带
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openhachimi_agent.service.browser.manager import BrowserManager


class _FakePage:
    def __init__(self, url="https://example.com", verdict=None):
        self.url = url
        self._closed = False
        self._verdict = verdict

    def is_closed(self):
        return self._closed

    async def evaluate(self, script, arg=None):
        return self._verdict


def _make_manager(verdict=None) -> BrowserManager:
    manager = BrowserManager.__new__(BrowserManager)
    manager.config = SimpleNamespace(browser_idle_timeout=0)
    manager._element_mappings = {}
    manager._active_mapping = {}
    manager._session_pages = {}
    manager._last_dialog = None
    manager._context_hardening_id = None
    manager._page = _FakePage(verdict=verdict)
    manager._context = None
    manager._captcha_detected_reason = None
    manager._captcha_setup_context_id = None
    manager._captcha_soft_note = None
    manager._op_lock = asyncio.Lock()
    return manager


@pytest.mark.asyncio
async def test_soft_widget_does_not_trigger_takeover():
    """小 widget（隐藏 reCAPTCHA 组件）不触发人工接管，只记 soft 提示。"""
    bm = _make_manager(verdict={"level": "soft", "reason": "widget_present: recaptcha"})
    result = await bm._detect_human_verification()
    assert result is None  # 不阻断
    assert bm._captcha_soft_note == "widget_present: recaptcha"


@pytest.mark.asyncio
async def test_hard_challenge_triggers_takeover():
    """大面积挑战框触发人工接管。"""
    bm = _make_manager(verdict={"level": "hard", "reason": "iframe_match: recaptcha"})
    result = await bm._detect_human_verification()
    assert result == "iframe_match: recaptcha"
    assert bm._captcha_detected_reason == "iframe_match: recaptcha"


@pytest.mark.asyncio
async def test_stale_observer_cache_cleared_by_active_check():
    """observer 上报的缓存被后续主动检测复核清除（防误报粘住）。"""
    bm = _make_manager(verdict=None)  # 主动检测：页面已无挑战
    bm._captcha_detected_reason = "iframe_match: recaptcha"  # observer 留下的缓存

    result = await bm._detect_human_verification()
    assert result is None  # 主动复核未确认 -> 清除缓存
    assert bm._captcha_detected_reason is None


@pytest.mark.asyncio
async def test_cache_cleared_when_challenge_resolved():
    """用户手动完成验证后（挑战卸载），缓存自动清除。"""
    bm = _make_manager(verdict={"level": "soft", "reason": "widget_present: .g-recaptcha"})
    bm._captcha_detected_reason = "iframe_match: recaptcha"  # 之前的挑战缓存

    result = await bm._detect_human_verification()
    assert result is None  # soft 不阻断
    assert bm._captcha_detected_reason is None  # hard 缓存已清
    assert bm._captcha_soft_note == "widget_present: .g-recaptcha"


@pytest.mark.asyncio
async def test_detection_failure_preserves_cache():
    """evaluate 本身失败（浏览器重连中）时保守返回缓存，不清除。"""
    bm = _make_manager()

    async def boom(*a, **k):
        raise RuntimeError("eval crashed")

    bm._page.evaluate = boom
    bm._captcha_detected_reason = "iframe_match: recaptcha"
    result = await bm._detect_human_verification()
    assert result == "iframe_match: recaptcha"  # 保守保留


@pytest.mark.asyncio
async def test_no_verdict_no_cache_is_clean():
    bm = _make_manager(verdict=None)
    assert await bm._detect_human_verification() is None
    assert bm._captcha_soft_note is None
