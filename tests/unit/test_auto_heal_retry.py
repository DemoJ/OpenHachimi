"""单测：auto_heal_retry 装饰器在重试耗尽时的契约。

约束：
- 失败字符串必须以 ``BROWSER_OP_FAILED:`` 开头，便于监控/自动化程序化识别。
- 成功路径必须直接返回原函数返回值。
- 仅对断连/会话崩溃类瞬时错误重试，其它异常立即失败。
"""

from __future__ import annotations

import logging

import pytest

from openhachimi_agent.service.browser.manager import auto_heal_retry


class _FakeBrowserManager:
    def __init__(self):
        self._page = None
        self.ensure_calls = 0

    async def _ensure_browser(self) -> None:
        # 健康路径：什么也不做，模拟重连成功
        self.ensure_calls += 1


@pytest.mark.asyncio
async def test_auto_heal_retry_returns_value_on_success():
    bm = _FakeBrowserManager()

    @auto_heal_retry(max_retries=3, base_delay=0.0)
    async def op(self) -> str:
        return "ok"

    result = await op(bm)
    assert result == "ok"
    assert bm.ensure_calls == 0  # 成功路径不应触发 heal


@pytest.mark.asyncio
async def test_auto_heal_retry_retries_disconnect_then_succeeds():
    """断连类错误（含 'not open' 关键词）应该被识别为瞬时错误并触发重试 + heal。"""
    bm = _FakeBrowserManager()
    calls = {"n": 0}

    @auto_heal_retry(max_retries=3, base_delay=0.0)
    async def op(self) -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("WebSocket connection is not open")
        return "recovered"

    result = await op(bm)
    assert result == "recovered"
    assert calls["n"] == 2
    assert bm.ensure_calls == 1  # 断连后调了一次 heal


@pytest.mark.asyncio
async def test_auto_heal_retry_non_transient_fails_fast(caplog):
    """非断连错误应该立即失败，不重试，避免对代码 bug 反复重试。"""
    bm = _FakeBrowserManager()
    calls = {"n": 0}

    @auto_heal_retry(max_retries=5, base_delay=0.0)
    async def boom(self) -> str:
        calls["n"] += 1
        raise KeyError("element_id not in mapping")

    with caplog.at_level(logging.ERROR, logger="openhachimi_agent.service.browser.manager"):
        result = await boom(bm)

    assert result.startswith("BROWSER_OP_FAILED:")
    assert calls["n"] == 1, "非瞬时错误必须立即失败，禁止重试"
    assert bm.ensure_calls == 0, "非瞬时错误不应触发 heal"
    # 日志必须带 traceback，方便事后排查代码 bug
    non_transient = [r for r in caplog.records if "Non-transient" in r.getMessage()]
    assert non_transient, "expected a Non-transient ERROR log line"
    assert non_transient[-1].exc_info is not None


@pytest.mark.asyncio
async def test_auto_heal_retry_disconnect_exhausted_returns_failure_marker(caplog):
    """断连错误重试耗尽后返回带前缀的失败字符串 + traceback 日志。"""
    bm = _FakeBrowserManager()

    @auto_heal_retry(max_retries=2, base_delay=0.0)
    async def boom(self) -> str:
        raise RuntimeError("connection is not open")

    with caplog.at_level(logging.ERROR, logger="openhachimi_agent.service.browser.manager"):
        result = await boom(bm)

    # 契约 1：失败字符串以约定前缀开头，方便程序化识别
    assert result.startswith("BROWSER_OP_FAILED:")
    # 契约 2：保留底层异常的可读信息
    assert "not open" in result
    # 契约 3：日志携带 traceback
    final_records = [r for r in caplog.records if r.levelno == logging.ERROR and "failed after" in r.getMessage()]
    assert final_records, "expected a final ERROR log line"
    assert final_records[-1].exc_info is not None, "final failure log must carry exc_info traceback"
