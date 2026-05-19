"""Streaming event bridge and queue consumption helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.service.agent_runtime.context import OperationState


logger = logging.getLogger(__name__)
STREAM_DONE = object()


@dataclass
class StreamStats:
    output_chars: int = 0
    chunk_count: int = 0
    first_chunk_ms: float | None = None
    last_chunk_preview: str = ""


class OperationStalledError(TimeoutError):
    def __init__(self, operation: str, stalled_for: float, timeout: int) -> None:
        self.operation = operation
        self.stalled_for = stalled_for
        self.timeout = timeout
        super().__init__(
            f"当前操作 {operation} 已 {stalled_for:.0f}s 没有可验证进展，超过 watchdog 阈值 {timeout}s。"
        )


def text_from_stream_event(event: object) -> str:
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    return ""


def summarize_tool_args(args: object, max_chars: int = 160) -> str:
    if args in (None, "", {}):
        return ""
    if isinstance(args, str):
        text = args
    else:
        import json

        try:
            text = json.dumps(args, ensure_ascii=False)
        except TypeError:
            text = str(args)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def status_from_stream_event(event: object) -> str:
    if isinstance(event, FunctionToolCallEvent):
        tool_name = event.part.tool_name
        args = summarize_tool_args(event.part.args_as_dict())
        if args:
            return f"\n[工具] 正在调用 {tool_name}：{args}\n"
        return f"\n[工具] 正在调用 {tool_name}\n"

    if isinstance(event, FunctionToolResultEvent):
        result = event.result
        outcome = getattr(result, "outcome", "success")
        if outcome == "success":
            return f"[工具] {result.tool_name} 完成\n"
        return f"[工具] {result.tool_name} 结束：{outcome}\n"

    return ""


def _update_operation_from_event(operation_state: OperationState, event: object) -> None:
    if isinstance(event, FunctionToolCallEvent):
        operation_state.start("tool", event.part.tool_name, summarize_tool_args(event.part.args_as_dict()))
        return
    if isinstance(event, FunctionToolResultEvent):
        operation_state.progress()
        operation_state.start("model", "executor")
        return
    if text_from_stream_event(event):
        operation_state.progress()


def build_stream_event_handler(
    stream_queue: asyncio.Queue[str | object],
    operation_state: OperationState,
) -> Callable[[object, object], Awaitable[None]]:
    async def handle_stream_events(_ctx: object, stream_event: object) -> None:
        async for event in stream_event:  # type: ignore[attr-defined]
            _update_operation_from_event(operation_state, event)
            if chunk := text_from_stream_event(event):
                await stream_queue.put(chunk)
            if status := status_from_stream_event(event):
                await stream_queue.put(status)

    return handle_stream_events


def _operation_timeout_seconds(operation_state: OperationState, config: AppConfig) -> int:
    name = operation_state.name or ""
    if operation_state.kind == "planner":
        return max(240, config.agent_timeout_seconds)
    if operation_state.kind == "model":
        return max(180, config.agent_timeout_seconds)
    if name.startswith("browser_"):
        return 120
    if name in {"web_fetch", "web_search", "discover_web_resources"}:
        return 90
    if name in {"read_file", "list_files", "find_files", "search_text", "git_status", "git_diff"}:
        return 120
    if name in {"run_command", "send_command_input", "command_status"}:
        return 60
    return max(120, config.stream_idle_timeout_seconds * 3)


def _stalled_for(operation_state: OperationState) -> float:
    return time.perf_counter() - operation_state.last_progress_at


def _raise_if_operation_stalled(operation_state: OperationState, config: AppConfig) -> None:
    timeout = _operation_timeout_seconds(operation_state, config)
    stalled_for = _stalled_for(operation_state)
    if stalled_for > timeout:
        raise OperationStalledError(operation_state.describe(), stalled_for, timeout)


def _heartbeat_message(operation_state: OperationState) -> str:
    operation_state.heartbeat_count += 1
    stalled_for = _stalled_for(operation_state)
    detail = f"，详情：{operation_state.detail}" if operation_state.detail else ""
    return (
        "\n[System] 当前任务仍在运行，"
        f"正在等待 {operation_state.describe()} 返回；已 {stalled_for:.0f}s 无新输出{detail}。\n"
    )


async def _cancel_stalled_task(task: asyncio.Task, exc: OperationStalledError) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    logger.error(
        "chat operation stalled operation=%s stalled_for=%.0f timeout=%d",
        exc.operation,
        exc.stalled_for,
        exc.timeout,
    )


async def consume_stream_queue(
    *,
    stream_queue: asyncio.Queue[str | object],
    task: asyncio.Task,
    config: AppConfig,
    role: str,
    session_id: str,
    start_time: float,
    stats: StreamStats,
    operation_state: OperationState,
) -> AsyncIterator[str]:
    while True:
        try:
            item = await asyncio.wait_for(
                stream_queue.get(),
                timeout=config.stream_idle_timeout_seconds,
            )
        except asyncio.TimeoutError:
            if task.done():
                break
            try:
                _raise_if_operation_stalled(operation_state, config)
            except OperationStalledError as exc:
                await _cancel_stalled_task(task, exc)
                raise
            yield _heartbeat_message(operation_state)
            continue

        if item is STREAM_DONE:
            break

        try:
            _raise_if_operation_stalled(operation_state, config)
        except OperationStalledError as exc:
            await _cancel_stalled_task(task, exc)
            raise

        chunk = str(item)
        compact_chunk = " ".join(chunk.split())
        if len(compact_chunk) > 120:
            compact_chunk = compact_chunk[:117] + "..."
        if compact_chunk:
            stats.last_chunk_preview = compact_chunk
        if stats.first_chunk_ms is None:
            stats.first_chunk_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "chat first chunk role=%s session_id=%s first_chunk_ms=%.0f chunk_chars=%d",
                role,
                session_id,
                stats.first_chunk_ms,
                len(chunk),
            )
        stats.chunk_count += 1
        stats.output_chars += len(chunk)
        yield chunk
