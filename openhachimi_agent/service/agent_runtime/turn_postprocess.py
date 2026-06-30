"""单轮对话结束后处理:持久化、上下文压缩、记忆抽取。

提供 ``run_turn`` 在 agent 执行完成后调用的纯函数:把本轮新增差量落库(含
系统级上下文快照 metadata)、按 token 用量判定并执行上下文压缩、抽取本轮
记忆。均接收 ``service`` 整体作参数,无 service 状态字段持有。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelRequest, UserPromptPart

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.memory.capture import capture_turn_memories
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.service.agent_runtime.context import AgentRunContext
from openhachimi_agent.service.agent_runtime.context_snapshot import (
    _USER_MESSAGE_METADATA_KEY,
    _snapshot_executor_context,
    _stamp_turn_metadata,
)
from openhachimi_agent.transport.api_models import ArtifactRef


if TYPE_CHECKING:
    from openhachimi_agent.service.agent_service import AgentService


logger = logging.getLogger(__name__)


def _resolve_final_output_text(result: object, result_holder: dict[str, object]) -> str:
    """把 result.output 解释为给用户看的文本:deferred 用 question,否则 str(output)。"""
    from pydantic_ai.tools import DeferredToolRequests

    raw_output = getattr(result, "output", "")
    if isinstance(raw_output, DeferredToolRequests):
        return result_holder.get("clarification_question") or "需要你提供更多信息以继续。"
    return str(raw_output or "")


def _collect_turn_artifacts(session_state: dict[str, object]) -> list[ArtifactRef]:
    """从 session_state.turn_artifacts 抽出 ArtifactRef 列表(过滤非 ArtifactRef 项)。"""
    return [
        artifact for artifact in session_state.get("turn_artifacts", [])
        if isinstance(artifact, ArtifactRef)
    ]


def _stamp_turn_context(
    service: "AgentService",
    new_history: list,
    deps: AgentDeps,
    *,
    role: str,
    message: str,
    history: list,
) -> None:
    """构造本轮 executor 系统级上下文快照并打 metadata(分 static_hash/dynamic 两段)。

    完整静态文本写到 ``service._context_static_pool``;``len(history)`` 是 prev_len
    的权威来源。这里不再产生新的 LLM 调用,executor agent 仅用来 introspect toolset。
    """
    _static_text = ""
    _dynamic_text = ""
    _static_hash = ""
    try:
        _executor_for_intro = service._get_agent(role, "main")
    except Exception:
        _executor_for_intro = None
    try:
        _static_text, _dynamic_text, _static_hash = _snapshot_executor_context(
            service.config, role, _executor_for_intro, deps, service=service
        )
    except Exception:
        logger.debug("system context snapshot failed", exc_info=True)
    if _static_hash and _static_text:
        try:
            service._ensure_context_static(_static_hash, _static_text)
        except Exception:
            logger.debug("failed to register static context to pool", exc_info=True)
    _stamp_turn_metadata(new_history, len(history), message, _dynamic_text, _static_hash)


async def _save_turn_messages(
    service: "AgentService",
    ctx: AgentRunContext,
    result: object,
    new_history: list,
    *,
    role: str,
    actual_session_id: str,
    latest_scope: str | None,
    resolved_channel_code: str | None,
    history: list,
) -> None:
    """更新压缩器 token 用量,并把本轮新增差量(new_history[len(history):])追加落库。

    append-only 语义下视图已折叠的中间段不重存;``_stamp_turn_metadata`` 已用
    ``len(history)`` 定位本轮新增起点,这里与之对齐。``result.usage`` 在当前
    pydantic-ai 是方法(需调用)而非属性,传其本身交由 update_from_response 解包 callable,
    避免库升级成 property 时炸,也防漏括号清零统计。
    """
    compressor = ctx.context_compressor
    if compressor is not None:
        try:
            compressor.update_from_response(result.usage)  # type: ignore[attr-defined]
        except Exception:
            logger.debug("context usage update failed", exc_info=True)
    new_slice = new_history[len(history):]
    await asyncio.to_thread(
        service.session_store.save_messages,
        role,
        actual_session_id,
        new_slice,
        scope=latest_scope,
        channel=resolved_channel_code,
        scope_key=latest_scope,
        append=True,
    )


async def _persist_turn(
    service: "AgentService",
    ctx: AgentRunContext,
    result: object,
    deps: AgentDeps,
    *,
    role: str,
    actual_session_id: str,
    latest_scope: str | None,
    resolved_channel_code: str | None,
    message: str,
    history: list,
) -> list:
    """构造本轮系统级上下文快照并打 metadata,再差量追加落库。返回 new_history。

    上下文压缩的 token 用量更新在此一并完成;压缩判定与落库见
    ``_maybe_compress_post_turn``。``len(history)`` 是 ``_stamp_turn_metadata``
    的 prev_len 与差量起点的权威来源,须用 load_context 返回的原视图长度。
    """
    new_history = list(result.all_messages())  # type: ignore[attr-defined]
    _stamp_turn_context(service, new_history, deps, role=role, message=message, history=history)
    await _save_turn_messages(
        service, ctx, result, new_history,
        role=role, actual_session_id=actual_session_id, latest_scope=latest_scope,
        resolved_channel_code=resolved_channel_code, history=history,
    )
    return new_history


async def _persist_failed_turn_user_message(
    service: "AgentService",
    ctx: AgentRunContext,
    *,
    role: str,
    actual_session_id: str,
    latest_scope: str | None,
    resolved_channel_code: str | None,
    user_message: str,
) -> None:
    """agent 调用失败兜底:把本轮用户输入落库,避免下一轮丢失上下文。

    正常路径下用户消息由 pydantic-ai 追加进 ``result.all_messages()``,再经
    ``_persist_turn`` 差量落库。但 agent 抛异常时 ``result`` 不可用,该 user 消息
    从未进入历史,下一轮 ``load_context`` 读不到它 → agent 表现为"忘了刚才聊什么"。
    本函数在失败时用一条 ``ModelRequest(UserPromptPart)`` 兜底落库,使下一轮能看到。

    幂等:append-only + ``INSERT OR IGNORE`` + ``MAX(turn_index)+1`` 续编;下一轮
    成功时 ``_persist_turn`` 的 ``new_history[len(history):]`` 会自然跳过已落库部分,
    不会重复。落库失败只 warn,绝不掩盖原始 agent 错误。
    """
    # clarify_user deferred 路径:用户回复以 deferred tool result 灌回模型,不应再
    # 作为独立 user 消息落库(否则下一轮会出现"用户消息 + 未消费 tool return"双入口)。
    if ctx.session_state.get("_user_clarification"):
        return
    if not (user_message or "").strip():
        return

    user_request = ModelRequest(
        parts=[UserPromptPart(content=user_message)],
        metadata={_USER_MESSAGE_METADATA_KEY: user_message},
    )
    # save_messages 是 append-only 追加语义:只传本轮新增的 user 消息,不能带 history
    # (否则会把已落库的历史再存一遍)。turn_index 由 MAX(turn_index)+1 续编。
    new_slice = [user_request]
    try:
        await asyncio.to_thread(
            service.session_store.save_messages,
            role,
            actual_session_id,
            new_slice,
            scope=latest_scope,
            channel=resolved_channel_code,
            scope_key=latest_scope,
            append=True,
        )
    except Exception:
        logger.warning(
            "failed-turn user message fallback persist failed role=%s session_id=%s",
            role, actual_session_id, exc_info=True,
        )
        return

    logger.info(
        "failed-turn user message persisted role=%s session_id=%s",
        role, actual_session_id,
    )


async def _maybe_compress_post_turn(
    service: "AgentService",
    compressor: object | None,
    *,
    role: str,
    actual_session_id: str,
    latest_scope: str | None,
) -> None:
    """压缩判定与落库:重读完整原始序列,compress 作用于 turn_index 升序的全量。

    边界内存下标 == turn_index,映射零误差。compressor 为 None 时直接返回。
    """
    if compressor is None or not compressor.should_compress():
        return
    try:
        _, full_raw = await asyncio.to_thread(
            service.session_store.load_messages, role, actual_session_id, latest_scope,
        )
        comp_result = await asyncio.to_thread(
            compressor.compress,
            full_raw,
            current_tokens=compressor.last_prompt_tokens,
        )
        if comp_result.dropped:
            await asyncio.to_thread(
                service.session_store.record_compression,
                role,
                actual_session_id,
                comp_result.head_end_idx,
                comp_result.tail_start_idx,
                comp_result.summary,
                total_len=len(full_raw),
            )
            logger.info(
                "context compressed post-turn role=%s session_id=%s head_end=%d tail_start=%d compression_count=%d",
                role, actual_session_id,
                comp_result.head_end_idx, comp_result.tail_start_idx,
                compressor.compression_count,
            )
        else:
            logger.info(
                "context compression skipped(no dropped) role=%s session_id=%s",
                role, actual_session_id,
            )
    except Exception:
        logger.warning(
            "context compression failed role=%s session_id=%s",
            role, actual_session_id, exc_info=True,
        )


async def _capture_turn_memory(
    service: "AgentService",
    *,
    role: str,
    actual_session_id: str,
    memory_scope: MemoryScope,
    effective_message: str,
    final_output_text: str,
    session_state: dict[str, object],
    memory_context: object,
    run_mode: str,
    start_time: float,
) -> None:
    """抽取本轮记忆:按 async_enabled 决定后台 fire-and-forget 或同步落库。"""
    capture_args = (service.config, memory_scope, effective_message, final_output_text)
    # turn 来源:scheduled / system 不进 L1 抽取(由 capture_turn_memories 处理)
    if run_mode == "scheduled":
        capture_source = "scheduled"
    elif run_mode == "system":
        capture_source = "system"
    else:
        capture_source = "user"
    capture_kwargs = {
        "task_frame": session_state.get("task_frame") if isinstance(session_state.get("task_frame"), dict) else None,
        "memory_context_ids": memory_context.ids,
        "duration_ms": int((time.perf_counter() - start_time) * 1000),
        "source": capture_source,
    }
    if service.config.memory.capture.async_enabled:
        async def _capture_memory_background() -> None:
            try:
                await asyncio.to_thread(capture_turn_memories, *capture_args, **capture_kwargs)
            except Exception:
                logger.exception("memory capture failed role=%s session_id=%s", role, actual_session_id)

        asyncio.create_task(_capture_memory_background())
    else:
        await asyncio.to_thread(capture_turn_memories, *capture_args, **capture_kwargs)
