"""单测：P3 新增能力契约。

覆盖：
- 会话路由：_session_route/_bind_session_page/_resolve_session_page
- 会话绑定生命周期：绑定失效自动清理 / close_tab 解绑
- trafilatura：未安装/失败时返回 None 走降级（mock ImportError）
- _extract_with_trafilatura 的线程提取 happy path（mock trafilatura）
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openhachimi_agent.service.browser.manager import BrowserManager


class _FakePage:
    def __init__(self, url="https://example.com", closed=False):
        self.url = url
        self._closed = closed

    def is_closed(self):
        return self._closed


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages


def _make_manager() -> BrowserManager:
    manager = BrowserManager.__new__(BrowserManager)
    manager.config = SimpleNamespace(browser_idle_timeout=0)
    manager._element_mappings = {}
    manager._active_mapping = {}
    manager._session_pages = {}
    manager._last_dialog = None
    manager._context_hardening_id = None
    manager._page = None
    manager._context = None
    manager._captcha_detected_reason = None
    manager._captcha_setup_context_id = None
    manager._op_lock = asyncio.Lock()
    return manager


# ---------- 会话路由 ----------

def test_session_route_none_session_keeps_global_page():
    bm = _make_manager()
    p = _FakePage()
    bm._page = p
    restore = bm._session_route(None)
    assert bm._page is p  # 无 session 不路由
    restore()
    assert bm._page is p


def test_session_route_switches_to_bound_page_and_restores():
    bm = _make_manager()
    global_page = _FakePage("https://global.com")
    session_page = _FakePage("https://session.com")
    bm._context = _FakeContext([global_page, session_page])
    bm._page = global_page
    bm._element_mappings = {id(global_page): {1: ([], "a")}, id(session_page): {2: ([], "b")}}
    bm._session_pages = {"s1": session_page}

    restore = bm._session_route("s1")
    assert bm._page is session_page  # 路由到会话页
    assert bm._active_mapping == {2: ([], "b")}  # 映射跟着切

    restore()
    assert bm._page is global_page  # 还原全局页
    assert bm._active_mapping == {1: ([], "a")}


def test_resolve_session_page_drops_stale_binding():
    bm = _make_manager()
    dead = _FakePage(closed=True)
    bm._session_pages = {"s1": dead}
    assert bm._resolve_session_page("s1") is None
    assert "s1" not in bm._session_pages  # 失效绑定被清理


def test_resolve_session_page_drops_page_not_in_valid_list():
    bm = _make_manager()
    orphan = _FakePage()  # 未 closed 但不在 context.pages 里
    bm._context = _FakeContext([])  # 空列表
    bm._session_pages = {"s1": orphan}
    assert bm._resolve_session_page("s1") is None
    assert "s1" not in bm._session_pages


def test_bind_session_page_none_unbinds():
    bm = _make_manager()
    p = _FakePage()
    bm._bind_session_page("s1", p)
    assert bm._session_pages["s1"] is p
    bm._bind_session_page("s1", None)
    assert "s1" not in bm._session_pages


def test_bind_session_page_ignores_none_session():
    bm = _make_manager()
    bm._bind_session_page(None, _FakePage())
    assert bm._session_pages == {}


# ---------- trafilatura 提取 ----------

@pytest.mark.asyncio
async def test_trafilatura_returns_none_when_not_installed(monkeypatch):
    bm = _make_manager()
    bm._page = _FakePage()

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "trafilatura":
            raise ImportError("No module named 'trafilatura'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert await bm._extract_with_trafilatura(1000) is None


@pytest.mark.asyncio
async def test_trafilatura_extracts_via_thread(monkeypatch):
    bm = _make_manager()
    bm._page = _FakePage("https://example.com/article")

    async def fake_content():
        return "<html><body><article>" + "正文内容。" * 100 + "</article></body></html>"

    bm._page.content = fake_content

    class _FakeTrafilatura:
        @staticmethod
        def extract(html, **kwargs):
            return "这是提取出的正文。" * 50

        @staticmethod
        def extract_metadata(html):
            return SimpleNamespace(title="测试标题", author="作者", date="2026-01-01")

    import sys
    monkeypatch.setitem(sys.modules, "trafilatura", _FakeTrafilatura)

    result = await bm._extract_with_trafilatura(100000)
    assert result is not None
    assert "测试标题" in result
    assert "这是提取出的正文" in result


@pytest.mark.asyncio
async def test_trafilatura_too_short_falls_back(monkeypatch):
    bm = _make_manager()
    bm._page = _FakePage()

    async def fake_content():
        return "<html>short</html>"

    bm._page.content = fake_content

    class _FakeTrafilatura:
        @staticmethod
        def extract(html, **kwargs):
            return "太短"  # < 80 字符

        @staticmethod
        def extract_metadata(html):
            return None

    import sys
    monkeypatch.setitem(sys.modules, "trafilatura", _FakeTrafilatura)
    assert await bm._extract_with_trafilatura(1000) is None


# ---------- occluder 检测（纯 mock 层面） ----------

@pytest.mark.asyncio
async def test_detect_occluder_returns_none_on_error():
    bm = _make_manager()
    bm._page = _FakePage()

    async def boom(*a, **k):
        raise RuntimeError("frame detached")

    bm._resolve_frame = boom
    assert await bm._detect_occluder(([], "#x")) is None
