"""单测：P1 新增浏览器能力契约。

覆盖：
- _normalize_url：localhost/内网 IP 默认 http
- select_option 匹配逻辑（text/value/宽松匹配/失败返回候选列表）
- press_key / hover / wait_for 参数校验
- _resolve_locator 的 framePath 链式解析
- go_back/go_forward 后元素映射失效
- GET_STATE_SCRIPT 含 shadow DOM / iframe 穿透关键代码
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openhachimi_agent.service.browser.dom_scripts import GET_STATE_SCRIPT
from openhachimi_agent.service.browser.manager import BrowserManager
from openhachimi_agent.service.browser.tabs import BrowserTabsMixin


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
    manager._op_lock = asyncio.Lock()
    return manager


# ---------- _normalize_url ----------

@pytest.mark.parametrize("url,expected", [
    ("example.com", "https://example.com"),
    ("http://example.com", "http://example.com"),
    ("https://example.com", "https://example.com"),
    ("localhost:3000", "http://localhost:3000"),
    ("127.0.0.1:8080/x", "http://127.0.0.1:8080/x"),
    ("192.168.1.5", "http://192.168.1.5"),
    ("10.0.0.2:9000", "http://10.0.0.2:9000"),
    ("file:///tmp/x.html", "file:///tmp/x.html"),
    ("about:blank", "about:blank"),
])
def test_normalize_url(url, expected):
    bm = _make_manager()
    assert bm._normalize_url(url) == expected


# ---------- select_option 匹配逻辑 ----------

OPTIONS = [
    {"value": "monthly", "text": "每月"},
    {"value": "yearly", "text": "每年"},
    {"value": "none", "text": "不订阅"},
]


class _SelectPage(_FakePage):
    """evaluate 返回固定 select 选项列表（通过 main_frame 转发）。"""

    def __init__(self, options=OPTIONS):
        super().__init__()
        self._options = options
        self.eval_args = []
        self.main_frame = SimpleNamespace(evaluate=self._evaluate)

    async def _evaluate(self, script, arg):
        self.eval_args.append(arg)
        return self._options


@pytest.mark.asyncio
async def test_select_option_matches_text_then_value():
    bm = _make_manager()
    page = _SelectPage()
    bm._page = page
    bm._active_mapping = {3: ([], "[data-agent-id='3']")}

    selected = []

    class _Locator:
        async def select_option(self, value, **kw):
            selected.append(value)

    async def fake_resolve(entry):
        return _Locator()

    bm._resolve_locator = fake_resolve

    raw = BrowserManager.select_option.__wrapped__
    result = await raw(bm, 3, "每月")
    assert "每月" in result and selected == ["monthly"]

    result = await raw(bm, 3, "yearly")
    assert "每年" in result and selected == ["monthly", "yearly"]

    # 宽松匹配（大小写/空白）
    result = await raw(bm, 3, "  不订阅  ")
    assert "不订阅" in result


@pytest.mark.asyncio
async def test_select_option_returns_candidates_on_mismatch():
    bm = _make_manager()
    bm._page = _SelectPage()
    bm._active_mapping = {3: ([], "[data-agent-id='3']")}

    async def fake_resolve(entry):
        class _Locator:
            async def select_option(self, value, **kw):
                raise AssertionError("不应触发选择")
        return _Locator()

    bm._resolve_locator = fake_resolve

    raw = BrowserManager.select_option.__wrapped__
    result = await raw(bm, 3, "季付")
    assert "没有匹配" in result
    assert "每月(monthly)" in result  # 候选列表格式


@pytest.mark.asyncio
async def test_select_option_rejects_non_select_element():
    bm = _make_manager()
    page = _SelectPage(options=None)
    bm._page = page
    bm._active_mapping = {3: ([], "[data-agent-id='3']")}

    async def fake_resolve(entry):
        return None

    bm._resolve_locator = fake_resolve

    raw = BrowserManager.select_option.__wrapped__
    result = await raw(bm, 3, "x")
    assert "不是原生" in result


# ---------- press_key 校验 ----------

@pytest.mark.asyncio
async def test_press_key_rejects_empty_key():
    bm = _make_manager()
    bm._page = _FakePage()
    bm._captcha_detected_reason = None

    async def noop_update():
        return None

    bm._update_active_page = noop_update
    bm._detect_human_verification = async_none

    raw = BrowserManager.press_key.__wrapped__
    result = await raw(bm, "  ")
    assert "不能为空" in result


async def async_none(*a, **k):
    return None


# ---------- wait_for ----------

@pytest.mark.asyncio
async def test_wait_for_fixed_seconds():
    bm = _make_manager()
    bm._page = _FakePage()

    raw = BrowserManager.wait_for.__wrapped__
    result = await raw(bm, text=None, seconds=0.1, timeout=1)
    assert "已等待" in result


@pytest.mark.asyncio
async def test_wait_for_text_found_and_timeout():
    bm = _make_manager()
    bm._page = _FakePage()

    async def evaluate(script, arg):
        return arg["text"] == "登录成功"

    bm._page.evaluate = evaluate

    raw = BrowserManager.wait_for.__wrapped__
    assert "已出现" in await raw(bm, text="登录成功", timeout=1)
    assert "超时" in await raw(bm, text="登录失败", timeout=1)


# ---------- go_back / go_forward 映射失效 ----------

class _HistoryPage(_FakePage):
    def __init__(self):
        super().__init__(url="https://example.com/page2")
        self.back_result = object()
        self.forward_result = object()

    async def go_back(self, **kw):
        self.url = "https://example.com/page1"
        return self.back_result

    async def go_forward(self, **kw):
        self.url = "https://example.com/page2"
        return self.forward_result


@pytest.mark.asyncio
async def test_go_back_invalidates_mapping():
    bm = _make_manager()
    page = _HistoryPage()
    bm._page = page
    bm._active_mapping = {1: ([], "x")}
    bm._captcha_detected_reason = None
    bm.wait_for_load_state = async_none
    bm._detect_human_verification = async_none
    bm._consume_dialog_report = lambda: ""

    raw = BrowserManager.go_back.__wrapped__
    result = await raw(bm)
    assert "已后退到" in result
    assert bm._active_mapping == {}  # 旧映射必须失效


@pytest.mark.asyncio
async def test_go_forward_at_history_end():
    bm = _make_manager()
    page = _HistoryPage()
    page.forward_result = None  # 无处可前进
    bm._page = page
    bm._captcha_detected_reason = None

    raw = BrowserManager.go_forward.__wrapped__
    result = await raw(bm)
    assert "无法继续前进" in result


# ---------- _resolve_locator frame 链 ----------

class _FrameChainPage(_FakePage):
    """模拟 page.locator / frame_locator 链式调用记录。"""

    def __init__(self):
        super().__init__()
        self.locator_calls = []
        self.frame_calls = []

    def locator(self, css):
        self.locator_calls.append(css)
        return SimpleNamespace(first=SimpleNamespace())

    def frame_locator(self, css):
        self.frame_calls.append(css)
        return self


@pytest.mark.asyncio
async def test_resolve_locator_main_document():
    bm = _make_manager()
    page = _FrameChainPage()
    bm._page = page

    await bm._resolve_locator(([], "[data-agent-id='7']"))
    assert page.locator_calls == ["[data-agent-id='7']"]
    assert page.frame_calls == []


@pytest.mark.asyncio
async def test_resolve_locator_nested_iframes():
    bm = _make_manager()
    page = _FrameChainPage()
    bm._page = page

    await bm._resolve_locator(([1, 2], "[data-agent-id='9']"))
    assert page.frame_calls == ["iframe[data-agent-frame='1']", "iframe[data-agent-frame='2']"]
    assert page.locator_calls == ["[data-agent-id='9']"]


# ---------- GET_STATE_SCRIPT 穿透能力静态检查 ----------

def test_get_state_script_traverses_shadow_dom():
    assert "node.shadowRoot" in GET_STATE_SCRIPT


def test_get_state_script_traverses_iframes():
    assert "contentDocument" in GET_STATE_SCRIPT
    assert "data-agent-frame" in GET_STATE_SCRIPT
    assert "framePath" in GET_STATE_SCRIPT
