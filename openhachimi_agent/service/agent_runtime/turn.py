"""单轮对话编排:主 agent 单次 run + verification 闸门 + 持久化。

Hermes 式重构后:不再有 router → planner → executor 多阶段串行。单条用户消息
直接喂给主 agent 的 ReAct 循环,失败由主 agent 自己 cancel/修订 todo,停止时
完整性校验由 factory 的 output_validator 闸门(verification_stop + final_verification
signal)一次完成。

状态字典仍挂在 ``AgentService`` 实例上(``_session_states`` / ``_running_tasks`` /
``_session_locks`` 等),本模块通过传入的 service 引用读写,以保持与既有测试兼容。

上下文快照构建见 ``context_snapshot``;流式末尾补发与 signal 渲染见
``turn_render``;后台 task 取消由本模块 ``_cancel_and_drain_task`` 提供。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING


from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.core.redaction import redact_text
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.service.agent_runtime.context import (
    AgentRunContext,
    mark_turn_finished,
    mark_turn_started,
)
from openhachimi_agent.service.agent_runtime.main_agent import (
    resume_main_agent,
    run_main_agent,
)
from openhachimi_agent.service.agent_runtime.streaming import STREAM_DONE, StreamStats
from openhachimi_agent.service.agent_runtime.turn_postprocess import (
    _capture_turn_memory,
    _collect_turn_artifacts,
    _maybe_compress_post_turn,
    _persist_turn,
    _resolve_final_output_text,
)
from openhachimi_agent.service.agent_runtime.turn_setup import (
    _build_run_context,
    _build_turn_deps,
    _prepare_turn_inputs,
    _refresh_mcp_once,
)
from openhachimi_agent.service.agent_runtime.turn_stream import (
    _consume_stream_or_raise,
    _finalize_non_stream,
    _yield_stream_terminal,
)
from openhachimi_agent.transport.api_models import ArtifactRef, AttachmentRef, ChatResponse


if TYPE_CHECKING:
    from openhachimi_agent.service.agent_service import AgentService


logger = logging.getLogger(__name__)


async def _cancel_and_drain_task(task: asyncio.Task, *, reason: str) -> None:
    """取消后台 agent task 并等待其清理完成。

    仅调用 ``task.cancel()`` 不等待时,流式客户端断开/async generator 被关闭会让
    pydantic-ai 的模型请求协程在事件循环关闭时仍处于 pending,进而触发
    "Task was destroyed but it is pending" 和 ContextVar token reset 报错。
    """
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        logger.debug("cancelled agent task finished with error reason=%s", reason, exc_info=True)


def _finalize_outcome(
    outcome: object,
    session_state: dict[str, object],
    deps: AgentDeps,
    result_holder: dict[str, object],
) -> None:
    """处理 DeferredToolRequests / final_verification_signal / 正常完成 三分支。

    写 result_holder。deferred 路径(clarify_user)只把 question 交给用户;
    final_verification_signal 路径把 signal 上抛给 turn_stream 渲染;正常路径直接
    把 result 落 result_holder。不再调 plan 状态机(plan_status 已废,主 agent 自主)。
    """
    from pydantic_ai.tools import DeferredToolRequests

    result_holder["result"] = outcome.result
    # deferred:本轮模型在调 clarify_user 时 pydantic-ai 把 DeferredToolRequests 作为
    # run output 返回。把待澄清问题交给用户;clarify_user 工具内部已按需挂起 plan。
    if isinstance(getattr(outcome.result, "output", None), DeferredToolRequests):
        pending = session_state.get("_user_clarification") or {}
        question_text = pending.get("question") or "需要你提供更多信息以继续。"
        result_holder["clarification_question"] = question_text
    elif outcome.final_verification_signal:
        result_holder["final_verification_signal"] = outcome.final_verification_signal
    # 正常完成:无额外动作


def _handle_run_agent_exception(
    exc: BaseException,
    session_state: dict[str, object],
    deps: AgentDeps,
    result_holder: dict[str, object],
    *,
    stream: bool,
    service: "AgentService",
    role: str,
    actual_session_id: str,
) -> None:
    """TimeoutError / 其它 Exception 分类,写 result_holder + 日志。

    CancelledError 由 ``run_agent_task`` 自行处理(透传 raise),不进此函数。
    plan 状态机已废,异常不再挂起/失败计划,只记日志 + 写 error。
    """
    del session_state, deps  # noqa: F841 — 保留入参以维持签名稳定,后续可复用
    if isinstance(exc, asyncio.TimeoutError):
        if stream:
            result_holder["error"] = TimeoutError(
                "Agent 执行超时:"
                f"{service.config.agent_timeout_seconds}s 内没有完成。"
                f"模型={service.config.model_name},"
                f"base_url={redact_text(service.config.openai_base_url or '默认')},"
                f"role={role},session_id={actual_session_id}。"
                "常见原因:模型服务无响应、工具调用卡住、浏览器/网络代理不可用。"
            )
            logger.exception(
                "chat timed out role=%s session_id=%s timeout_seconds=%d stream=true",
                role, actual_session_id, service.config.agent_timeout_seconds,
            )
        else:
            result_holder["error"] = exc
            logger.exception("chat timed out role=%s session_id=%s stream=false", role, actual_session_id)
        return
    # 兜底:其它异常
    result_holder["error"] = exc
    logger.exception(
        "chat failed role=%s session_id=%s stream=%s",
        role, actual_session_id, str(stream).lower(),
    )


async def _resolve_turn_outcome(
    service: "AgentService",
    ctx: AgentRunContext,
    deps: AgentDeps,
    refreshed: list[bool],
    result_holder: dict[str, object],
) -> None:
    """deferred resume 或主 agent 单次 run,再 finalize outcome 写入 result_holder。

    clarify_user 上一轮若留下 ``_user_clarification`` 标志,本轮直接 deferred resume
    把用户回复灌回 graph;否则走主 agent 单次 run。无 router/planner 前置分流。
    """
    session_state = ctx.session_state
    outcome = None
    if session_state.get("_user_clarification"):
        await _refresh_mcp_once(service, ctx, deps, refreshed)
        outcome = await resume_main_agent(ctx, service._get_agent)
        # 状态损坏导致无法 resume → outcome=None,fall through 到正常流程。

    if outcome is None:
        await _refresh_mcp_once(service, ctx, deps, refreshed)
        outcome = await run_main_agent(ctx, service._get_agent)
    _finalize_outcome(outcome, session_state, deps, result_holder)


async def _run_agent_task(
    service: "AgentService",
    ctx: AgentRunContext,
    deps: AgentDeps,
    refreshed: list[bool],
    result_holder: dict[str, object],
    *,
    stream: bool,
) -> None:
    """本轮 agent 执行协程:解析 outcome + 异常分类 + finally 收尾。

    CancelledError 透传(由 run_turn 的 try/finally 清理 task);其余异常经
    ``_handle_run_agent_exception`` 写入 result_holder。finally 保证 mark_turn_finished
    与(流式时)STREAM_DONE 入队。
    """
    role = ctx.role
    actual_session_id = ctx.session_id
    mark_turn_started(ctx.session_state)
    try:
        await _resolve_turn_outcome(service, ctx, deps, refreshed, result_holder)
    except asyncio.CancelledError:
        logger.info(
            "chat stream cancelled role=%s session_id=%s" if stream else "chat cancelled role=%s session_id=%s stream=false",
            role, actual_session_id,
        )
        raise
    except Exception as exc:
        _handle_run_agent_exception(
            exc, ctx.session_state, deps, result_holder,
            stream=stream, service=service, role=role, actual_session_id=actual_session_id,
        )
    finally:
        mark_turn_finished(ctx.session_state)
        if stream:
            with contextlib.suppress(asyncio.CancelledError):
                await ctx.stream_queue.put(STREAM_DONE)  # type: ignore[arg-type]


@dataclass
class _TurnRunState:
    """``run_turn`` 跨子函数共享的单轮运行态聚合,避免长参数列表。

    ``async with lock`` 内构造,``_run_turn_locked`` 据此编排 stream/sync 分支、
    后置持久化与收尾 yield。
    """
    service: "AgentService"
    inputs: SimpleNamespace
    role: str
    actual_session_id: str
    history: list
    message: str
    stream: bool
    run_mode: str
    start_time: float
    session_state: dict[str, object]
    deps: AgentDeps
    memory_scope: MemoryScope
    memory_context: object
    ctx: AgentRunContext
    stream_queue: asyncio.Queue
    stream_stats: StreamStats
    result_holder: dict[str, object]


async def _finalize_turn_data(
    state: _TurnRunState, result: object
) -> tuple[str, list, list[ArtifactRef]]:
    """后置无 yield 段:解析 final_output_text + 落库 + 压缩判定 + 记忆抽取。

    返回 (final_output_text, new_history, turn_artifacts) 供收尾 yield 使用。
    """
    service = state.service
    ctx = state.ctx
    deps = state.deps
    session_state = state.session_state
    role = state.role
    actual_session_id = state.actual_session_id
    final_output_text = _resolve_final_output_text(result, state.result_holder)
    turn_artifacts = _collect_turn_artifacts(session_state)
    service.register_artifacts(turn_artifacts)
    new_history = await _persist_turn(
        service, ctx, result, deps,
        role=role, actual_session_id=actual_session_id, latest_scope=state.inputs.latest_scope,
        resolved_channel_code=state.inputs.resolved_channel_code, message=state.message, history=state.history,
    )
    await _maybe_compress_post_turn(
        service, ctx.context_compressor,
        role=role, actual_session_id=actual_session_id, latest_scope=state.inputs.latest_scope,
    )
    await _capture_turn_memory(
        service,
        role=role, actual_session_id=actual_session_id, memory_scope=state.memory_scope,
        effective_message=state.inputs.effective_message, final_output_text=final_output_text,
        session_state=session_state, memory_context=state.memory_context,
        run_mode=state.run_mode, start_time=state.start_time,
    )
    return final_output_text, new_history, turn_artifacts


async def _yield_turn_finish(
    state: _TurnRunState,
    final_output_text: str,
    new_history: list,
    turn_artifacts: list[ArtifactRef],
) -> AsyncIterator[object]:
    """收尾 yield:流式末尾补发 + finished 日志,或非流式 ChatResponse(含 signal 拼接)。"""
    if state.stream:
        async for event in _yield_stream_terminal(
            role=state.role, actual_session_id=state.actual_session_id, result_holder=state.result_holder,
            final_output_text=final_output_text, stream_stats=state.stream_stats,
            new_history=new_history, start_time=state.start_time,
        ):
            yield event
    else:
        async for event in _finalize_non_stream(
            role=state.role, actual_session_id=state.actual_session_id, result_holder=state.result_holder,
            final_output_text=final_output_text, turn_artifacts=turn_artifacts,
            new_history=new_history, start_time=state.start_time,
        ):
            yield event


async def _run_turn_locked(state: _TurnRunState) -> AsyncIterator[object]:
    """``async with lock`` 内的单轮编排:调度 agent task → stream/sync 分支 → 后置持久化 → 收尾 yield。

    task 在本函数内创建并登记到 ``service._running_tasks``;finally 负责摘除并兜底取消。
    """
    service = state.service
    ctx = state.ctx
    actual_session_id = state.actual_session_id
    stream = state.stream
    task = asyncio.create_task(
        _run_agent_task(service, ctx, state.deps, [False], state.result_holder, stream=stream)
    )
    service._running_tasks[actual_session_id] = task

    try:
        if stream:
            async for event in _consume_stream_or_raise(
                service,
                ctx=ctx, task=task, stream_queue=state.stream_queue, stream_stats=state.stream_stats,
                result_holder=state.result_holder, deps=state.deps, session_state=state.session_state,
                role=state.role, actual_session_id=actual_session_id, start_time=state.start_time,
            ):
                yield event
        else:
            try:
                await task
            except asyncio.CancelledError:
                if task.cancelled():
                    yield ChatResponse(output="【任务已被手动中断】", role=state.role, session_id=actual_session_id)
                    return
                raise
            if error := state.result_holder.get("error"):
                raise error

        final_output_text, new_history, turn_artifacts = await _finalize_turn_data(
            state, state.result_holder["result"]
        )
        async for event in _yield_turn_finish(state, final_output_text, new_history, turn_artifacts):
            yield event
    finally:
        service._running_tasks.pop(actual_session_id, None)
        if not task.done():
            await _cancel_and_drain_task(task, reason="run_turn_finally")


async def run_turn(
    service: "AgentService",
    message: str,
    role: str | None,
    session_id: str | None,
    *,
    stream: bool,
    attachments: Sequence[AttachmentRef] | None = None,
    run_mode: str = "interactive",
    channel_context: dict[str, object] | None = None,
    scheduler_context: dict[str, object] | None = None,
    channel: str | None = None,
    delivery_target: dict[str, object] | None = None,
) -> AsyncIterator[object]:
    """运行单轮对话,产出流式事件或最终 ChatResponse。状态由 ``service`` 持有,行为与原 ``_run_with_session`` 一致。"""
    start_time = time.perf_counter()
    inputs = _prepare_turn_inputs(
        service, message, role, session_id,
        attachments=attachments, channel_context=channel_context,
        scheduler_context=scheduler_context, channel=channel, delivery_target=delivery_target,
    )
    actual_session_id, history = service.session_store.load_context(
        inputs.role, inputs.session_id, inputs.latest_scope
    )

    async with service._get_session_lock(actual_session_id):
        logger.info(
            "chat started role=%s session_id=%s message_chars=%d history_messages=%d attachment_count=%d stream=%s",
            inputs.role, actual_session_id, len(message), len(history),
            len(inputs.attachment_list), str(stream).lower(),
        )
        await service._maybe_reload_mcp_toolsets()
        session_state = service._session_states.setdefault(actual_session_id, {})
        session_state["turn_artifacts"] = []
        deps, memory_scope, memory_context = _build_turn_deps(service, inputs, session_state, run_mode=run_mode)
        ctx, stream_queue, stream_stats, result_holder = _build_run_context(
            service, inputs, history, deps, session_state, stream=stream, message=message,
        )
        state = _TurnRunState(
            service=service, inputs=inputs, role=inputs.role, actual_session_id=actual_session_id,
            history=history, message=message, stream=stream, run_mode=run_mode, start_time=start_time,
            session_state=session_state, deps=deps, memory_scope=memory_scope, memory_context=memory_context,
            ctx=ctx, stream_queue=stream_queue, stream_stats=stream_stats, result_holder=result_holder,
        )
        async for event in _run_turn_locked(state):
            yield event
