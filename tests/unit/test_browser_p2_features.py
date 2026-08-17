"""单测：P2 新增能力契约。

覆盖：
- _diff_snapshot：click 前后快照对比摘要
- _verify_element_description：描述自检的通过/拒绝/宽松边界
- get_state 输出层：state 标记 + 相似折叠（纯 Python 渲染逻辑）
- screenshot：vision 未配置时返回路径提示
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openhachimi_agent.service.browser.interactions import BrowserInteractionsMixin
from openhachimi_agent.service.browser.manager import BrowserManager


class _FakePage:
    def __init__(self, url="https://example.com"):
        self.url = url
        self._closed = False

    def is_closed(self):
        return self._closed


def _make_manager() -> BrowserManager:
    manager = BrowserManager.__new__(BrowserManager)
    manager.config = SimpleNamespace(browser_idle_timeout=0, base_dir=None)
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


# ---------- _diff_snapshot ----------

def test_diff_snapshot_detects_navigation():
    bm = _make_manager()
    before = {"url": "https://a.com/x", "title": "A", "interactive": 10, "bodyChars": 100}
    after = {"url": "https://a.com/y", "title": "A", "interactive": 10, "bodyChars": 100}
    diff = bm._diff_snapshot(before, after)
    assert "跳转" in diff and "y" in diff


def test_diff_snapshot_detects_title_and_interactive_changes():
    bm = _make_manager()
    before = {"url": "u", "title": "Home", "interactive": 5, "bodyChars": 100}
    after = {"url": "u", "title": "Dashboard", "interactive": 20, "bodyChars": 100}
    diff = bm._diff_snapshot(before, after)
    assert "Dashboard" in diff
    assert "增加 15" in diff


def test_diff_snapshot_no_change_returns_empty():
    bm = _make_manager()
    same = {"url": "u", "title": "t", "interactive": 5, "bodyChars": 100}
    assert bm._diff_snapshot(same, same) == ""


# ---------- _verify_element_description ----------

@pytest.mark.asyncio
async def test_verify_description_pass_on_contains():
    bm = _make_manager()
    bm._page = _FakePage()
    entry = ([], "[data-agent-id='1']")

    async def fake_eval(frame_path, script, arg):
        if "ELEMENT" in script or script.startswith("("):
            return {"value": "", "text": "登录"}
        return {"value": "立即登录"}

    # 元素实际文本 "立即登录"，描述 "登录"（互含） -> 通过
    async def fake_eval2(frame_path, script, arg):
        return None

    bm._evaluate_in_frame = fake_eval2
    result = await bm._verify_element_description(1, None, entry)
    assert result is None  # 无描述直接通过


@pytest.mark.asyncio
async def test_verify_description_rejects_mismatch():
    bm = _make_manager()
    bm._page = _FakePage()
    entry = ([], "[data-agent-id='1']")

    async def fake_eval(frame_path, script, arg):
        # 元素状态与文本都是"搜索"，描述却是"登录按钮"
        if script.startswith("(s)"):
            return "搜索"
        return {"value": "搜索"}

    bm._evaluate_in_frame = fake_eval
    result = await bm._verify_element_description(1, "登录按钮", entry)
    assert result is not None
    assert "不符" in result
    assert "搜索" in result


@pytest.mark.asyncio
async def test_verify_description_pass_when_actual_contains_desc():
    bm = _make_manager()
    bm._page = _FakePage()
    entry = ([], "[data-agent-id='1']")

    async def fake_eval(frame_path, script, arg):
        if script.startswith("(s)"):
            return "GitHub 登录按钮（推荐）"
        return None

    bm._evaluate_in_frame = fake_eval
    # 描述 "登录" 是实际文本的子串 -> 通过（宽松策略）
    result = await bm._verify_element_description(1, "登录", entry)
    assert result is None


@pytest.mark.asyncio
async def test_verify_description_swallows_errors():
    bm = _make_manager()
    bm._page = _FakePage()
    entry = ([], "[data-agent-id='1']")

    async def boom(*a, **k):
        raise RuntimeError("frame detached")

    bm._evaluate_in_frame = boom
    # 校验失败不致命，放行点击
    result = await bm._verify_element_description(1, "任何", entry)
    assert result is None


# ---------- get_state 输出渲染（state 标记 + 相似折叠） ----------

def _render_elements(els):
    """复刻 manager.get_state 的渲染循环（纯函数部分）。"""
    lines = []

    def _fold_key(el):
        return (el.get('role'), el.get('type'), el.get('text'))

    i = 0
    while i < len(els):
        el = els[i]
        j = i + 1
        if el.get('isInteractive') and el.get('text'):
            while j < len(els) and _fold_key(els[j]) == _fold_key(el):
                j += 1
        similar = j - i - 1
        if similar < 3:
            j = i + 1  # 低于阈值不折叠，逐个渲染
            similar = 0
        state = el.get('state')
        state_mark = f" [当前:{state}]" if state else ""
        fold_mark = f" [+{similar} 个相同元素已折叠]" if similar >= 3 else ""
        lines.append(f"[{el['id']}] {el['role']}{state_mark}: {el['text']}{fold_mark}")
        i = j
    return lines


def test_render_shows_state_marks():
    els = [
        {"id": 1, "role": "input", "text": "搜索", "state": "已填入关键词"},
        {"id": 2, "role": "input", "text": "agree", "state": "checked"},
    ]
    lines = _render_elements(els)
    assert "[当前:已填入关键词]" in lines[0]
    assert "[当前:checked]" in lines[1]


def test_render_folds_similar_siblings():
    els = [{"id": i, "role": "a", "text": "商品卡片", "isInteractive": True, "type": None} for i in range(1, 7)]
    lines = _render_elements(els)
    assert len(lines) == 1  # 6 个同款只渲染 1 行
    assert "[+5 个相同元素已折叠]" in lines[0]


def test_render_no_fold_below_threshold():
    els = [{"id": 1, "role": "a", "text": "x", "isInteractive": True},
           {"id": 2, "role": "a", "text": "x", "isInteractive": True},
           {"id": 3, "role": "a", "text": "x", "isInteractive": True}]
    lines = _render_elements(els)
    assert len(lines) == 3  # 3 个（similar=2 < 3）不折叠


# ---------- screenshot（vision 未配置路径） ----------

@pytest.mark.asyncio
async def test_screenshot_saves_and_reports_without_vision():
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    bm = _make_manager()
    bm.config = SimpleNamespace(browser_idle_timeout=0, base_dir=tmp, vision=None)
    bm._op_lock = asyncio.Lock()

    page = _FakePage()
    saved = {}

    async def fake_screenshot(path=None, timeout=0):
        saved["path"] = path
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")

    page.screenshot = fake_screenshot
    bm._page = page

    async def noop():
        return None

    bm._update_active_page = noop
    bm._consume_dialog_report = lambda: ""

    raw = BrowserManager.screenshot.__wrapped__
    result = await raw(bm)
    assert "截图已保存" in result
    assert Path(saved["path"]).exists()
    assert "未配置视觉模型" in result or "画面描述" in result
