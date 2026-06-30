"""单轮对话的流式/非流式收尾事件产出。

提供 ``run_turn`` 在 agent 执行完成后用来消费流队列、渲染末尾补发与 signal、
产出最终 ChatResponse 的 async generator。均无 service 状态字段持有,通过
显式参数接收 ctx / result_holder / stream_stats 等。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.core.redaction import redact_exception
from openhachimi_agent.service.agent_runtime.commands import SIGNAL_LABELS
from openhachimi_agent.service.agent_runtime.context import (
    AgentRunContext,
    has_active_todos,
    suspend_current_plan,
)
from openhachimi_agent.service.agent_runtime.streaming import (
    OperationStalledError,
    StreamEventItem,
    StreamStats,
    consume_stream_queue,
    system_stream_event,
)
from openhachimi_agent.service.agent_runtime.turn_postprocess import _collect_turn_artifacts
from openhachimi_agent.service.agent_runtime.turn_render import (
    _format_signal_for_user,
    _resolve_terminal_stream_text,
)
from openhachimi_agent.transport.api_models import ArtifactRef, ChatResponse


if TYPE_CHECKING:
    from openhachimi_agent.service.agent_service import AgentService


logger = logging.getLogger(__name__)


def _stall_event(
    exc: OperationStalledError,
    session_state: dict[str, object],
    deps: AgentDeps,
) -> object | None:
    """处理 OperationStalledError:有活动 todos 则挂起计划,返回要 yield 的系统提示。

    Hermes 式重构后不再 fail plan(plan 状态机已简化):无活动 todos 时直接给
    "任务未完成"提示,下一轮主 agent 重新理解用户请求。
    """
    stalled_detail = {"operation": exc.operation, "stalled_for": exc.stalled_for, "timeout": exc.timeout}
    if has_active_todos(session_state):
        suspend_current_plan(session_state, reason="operation_stalled", detail=stalled_detail, deps=deps)
        return system_stream_event(
            "\n\n[System] 当前任务已暂停:"
            f"{exc} 旧计划已挂起,不会影响下一轮对话;"
            "如需恢复,请明确说明\"继续刚才的任务\"。"
        )
    return system_stream_event(f"\n\n[System] 当前任务未完成:{exc} 未生成可恢复计划,下一轮将重新理解用户请求。")


async def _iter_post_completion(
    service: "AgentService",
    *,
    task: asyncio.Task,
    result_holder: dict[str, object],
    session_state: dict[str, object],
) -> AsyncIterator[object]:
    """task 完成后:cancel 检查 → error raise → signal 渲染 → artifacts 去重 yield。"""
    try:
        await task
    except asyncio.CancelledError:
        if task.cancelled():
            yield system_stream_event("\n\n【任务已被手动中断】")
            return
        raise

    if error := result_holder.get("error"):
        raise RuntimeError(f"Agent 调用失败:{redact_exception(error)}") from error
    for signal_key, _signal_label in SIGNAL_LABELS:
        if signal_value := result_holder.get(signal_key):
            rendered = _format_signal_for_user(signal_key, signal_value)
            if rendered:
                yield system_stream_event(f"\n\n{rendered}\n")
    turn_artifacts = _collect_turn_artifacts(session_state)
    service.register_artifacts(turn_artifacts)
    seen_artifacts: set[str] = set()
    for artifact in turn_artifacts:
        if artifact.id in seen_artifacts:
            continue
        seen_artifacts.add(artifact.id)
        yield StreamEventItem(
            type="artifact",
            text=f"已生成文件:{artifact.filename}",
            artifact=artifact,
            counted_as_output=False,
        )


async def _consume_stream_or_raise(
    service: "AgentService",
    *,
    ctx: AgentRunContext,
    task: asyncio.Task,
    stream_queue: asyncio.Queue,
    stream_stats: StreamStats,
    result_holder: dict[str, object],
    deps: AgentDeps,
    session_state: dict[str, object],
    role: str,
    actual_session_id: str,
    start_time: float,
) -> AsyncIterator[object]:
    """流式分支:消费 stream_queue,处理 stall/cancel/error,再 yield signal + artifacts。

    OperationStalledError 与 task 被 cancel 时 yield 系统提示后 return;result_holder
    含 error 时 raise RuntimeError。
    """
    try:
        async for event in consume_stream_queue(
            stream_queue=stream_queue,
            task=task,
            config=service.config,
            role=role,
            session_id=actual_session_id,
            start_time=start_time,
            stats=stream_stats,
            operation_state=ctx.operation_state,
        ):
            yield event
    except OperationStalledError as exc:
        if (event := _stall_event(exc, session_state, deps)) is not None:
            yield event
        return

    async for event in _iter_post_completion(
        service, task=task, result_holder=result_holder, session_state=session_state,
    ):
        yield event


async def _yield_stream_terminal(
    *,
    role: str,
    actual_session_id: str,
    result_holder: dict[str, object],
    final_output_text: str,
    stream_stats: StreamStats,
    new_history: list,
    start_time: float,
) -> AsyncIterator[object]:
    """流式末尾补发 text(覆盖 deferred 与整轮零 chunk 两个场景)+ finished 日志。"""
    terminal_text = _resolve_terminal_stream_text(
        final_output_text, result_holder, stream_stats.chunk_count,
    )
    if terminal_text:
        is_clarify_deferred = "clarification_question" in result_holder
        stream_stats.output_chars += len(terminal_text)
        stream_stats.chunk_count += 1
        logger.info(
            "chat produced non-streamed output role=%s session_id=%s output_chars=%d deferred=%s",
            role, actual_session_id, len(terminal_text), str(is_clarify_deferred).lower(),
        )
        yield StreamEventItem(type="text", text=terminal_text)

    logger.info(
        "chat finished role=%s session_id=%s output_chars=%d chunks=%d first_chunk_ms=%s history_messages=%d duration_ms=%.0f stream=true",
        role, actual_session_id,
        stream_stats.output_chars, stream_stats.chunk_count,
        f"{stream_stats.first_chunk_ms:.0f}" if stream_stats.first_chunk_ms is not None else None,
        len(new_history), (time.perf_counter() - start_time) * 1000,
    )


async def _finalize_non_stream(
    *,
    role: str,
    actual_session_id: str,
    result_holder: dict[str, object],
    final_output_text: str,
    turn_artifacts: list[ArtifactRef],
    new_history: list,
    start_time: float,
) -> AsyncIterator[object]:
    """非流式:finished 日志 + ChatResponse(含 signal 拼接)。"""
    logger.info(
        "chat finished role=%s session_id=%s output_chars=%d history_messages=%d duration_ms=%.0f stream=false",
        role, actual_session_id, len(final_output_text), len(new_history),
        (time.perf_counter() - start_time) * 1000,
    )
    output = final_output_text
    for signal_key, _signal_label in SIGNAL_LABELS:
        if signal_value := result_holder.get(signal_key):
            rendered = _format_signal_for_user(signal_key, signal_value)
            if rendered:
                output = f"{output}\n\n{rendered}"
    yield ChatResponse(
        output=output,
        role=role,
        session_id=actual_session_id,
        artifacts=turn_artifacts,
    )
