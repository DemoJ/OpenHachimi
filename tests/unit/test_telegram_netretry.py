"""单测：``_retry_network_call`` 对 Telegram 瞬时网络错误的容错契约。

约束：
- 对 ``NetworkError`` / ``TimedOut`` 重试，重试期内恢复则正常返回。
- 重试耗尽仍失败时抛出最后一次原异常，交给上层 fallback。
- ``BadRequest`` 等确定性错误立即抛出，禁止重试。
- ``RetryAfter``（限流）按服务端给定秒数 sleep 后重试一次。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from openhachimi_agent.interface.telegram import _retry_network_call


async def _noop_sleep(*_args: Any, **_kw: Any) -> None:
    """独立 sleep 替身：不回调 asyncio.sleep，避免 monkeypatch 递归。"""
    return None


def _coro_factory(side_effects: list[Any]):
    """构造一个按顺序抛出/返回的协程工厂，便于重试时重建协程。"""
    state = {"i": 0}

    async def call() -> str:
        i = state["i"]
        state["i"] += 1
        effect = side_effects[i]
        if isinstance(effect, BaseException):
            raise effect
        return effect

    return call


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_error(monkeypatch):
    """第一次抖动 NetworkError，第二次成功 → 返回结果。"""
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    factory = _coro_factory([NetworkError("httpx.ConnectError: "), "ok"])

    result = await _retry_network_call(factory, max_retries=3)

    assert result == "ok"


@pytest.mark.asyncio
async def test_retry_succeeds_after_timed_out(monkeypatch):
    """TimedOut 同样属于瞬时错误，应当重试。"""
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    factory = _coro_factory([TimedOut(), TimedOut(), "recovered"])

    result = await _retry_network_call(factory, max_retries=3)

    assert result == "recovered"


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_error(monkeypatch, caplog):
    """重试耗尽仍 NetworkError → 抛出原异常，由上层处理。"""
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    factory = _coro_factory([NetworkError("boom-1"), NetworkError("boom-2"), NetworkError("boom-3")])

    with caplog.at_level(logging.DEBUG, logger="openhachimi_agent.interface.telegram"):
        with pytest.raises(NetworkError) as exc_info:
            await _retry_network_call(factory, max_retries=3)

    # 抛出的是最后一次的原异常，而非被包装
    assert "boom-3" in str(exc_info.value)
    # 重试期间应有 debug 日志（验证确实重试了 2 次）
    retry_logs = [r for r in caplog.records if "retry" in r.getMessage()]
    assert len(retry_logs) == 2


@pytest.mark.asyncio
async def test_bad_request_fails_immediately(monkeypatch):
    """BadRequest 是确定性错误，立即抛出，不重试。"""
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    factory = _coro_factory([BadRequest("message is too long")])

    with pytest.raises(BadRequest):
        await _retry_network_call(factory, max_retries=3)


@pytest.mark.asyncio
async def test_retry_after_sleeps_then_retries(monkeypatch):
    """RetryAfter（限流）按服务端秒数 sleep 后重试，不受 max_retries 约束。"""
    slept: list[float] = []

    async def fake_sleep(delay: float, *_: Any) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    factory = _coro_factory([RetryAfter(5), "ok-after-limit"])

    result = await _retry_network_call(factory, max_retries=3)

    assert result == "ok-after-limit"
    # 必须按服务端要求等待 5 秒后再重试
    assert slept == [5.0]


@pytest.mark.asyncio
async def test_retry_after_with_no_max_retries_budget(monkeypatch):
    """即便 max_retries=1，RetryAfter 仍应重试一次（限流专用通道，不计入 max_retries）。"""
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    factory = _coro_factory([RetryAfter(1), "ok"])

    result = await _retry_network_call(factory, max_retries=1)

    assert result == "ok"
