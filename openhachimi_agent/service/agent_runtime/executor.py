"""Executor orchestration with replan and final verification repair."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.usage import UsageLimits

from openhachimi_agent.agent.execution import get_final_verification_signal, get_ledger_length, get_replan_signal
from openhachimi_agent.agent.factory import build_scheduled_executor_agent
from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.service.agent_runtime.context import (
    AgentRunContext,
    has_active_todos,
    has_restorable_suspended_plan,
    restore_suspended_plan,
)
from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem
from openhachimi_agent.transport.api_models import AttachmentRef
from openhachimi_agent.vision.capabilities import mark_model_vision_support
from openhachimi_agent.vision.preprocess import VisionPreprocessResult, image_attachments, preprocess_vision_attachments
from openhachimi_agent.tools.vision_guard import normalize_vision_guard_path


logger = logging.getLogger(__name__)


def _normalize_attachment_path(config: AppConfig, attachment: AttachmentRef) -> str:
    path = Path(attachment.local_path)
    target = path if path.is_absolute() else config.base_dir / path
    return normalize_vision_guard_path(target)


def _vision_state_entry(config: AppConfig, attachment: AttachmentRef, *, status: str, mode: str | None = None) -> dict[str, Any]:
    resolved_path = _normalize_attachment_path(config, attachment)
    return {
        "attachment_id": attachment.id,
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "kind": attachment.kind,
        "local_path": attachment.local_path,
        "resolved_path": resolved_path,
        "mode": mode,
        "status": status,
        "model": None,
        "summary": None,
        "errors": [],
        "size_bytes": attachment.size_bytes,
    }


def _mark_vision_processing(session_state: dict[str, Any], config: AppConfig, attachments: list[AttachmentRef]) -> None:
    images = image_attachments(attachments)
    if not images:
        return
    vision_attachments = session_state.setdefault("vision_attachments", {})
    path_index = session_state.setdefault("vision_attachment_paths", {})
    for attachment in images:
        entry = _vision_state_entry(config, attachment, status="processing")
        vision_attachments[attachment.id] = entry
        path_index[entry["resolved_path"]] = attachment.id


def _mark_vision_result(session_state: dict[str, Any], config: AppConfig, attachments: list[AttachmentRef], vision_result: VisionPreprocessResult) -> None:
    images_by_id = {attachment.id: attachment for attachment in image_attachments(attachments)}
    vision_attachments = session_state.setdefault("vision_attachments", {})
    path_index = session_state.setdefault("vision_attachment_paths", {})

    for status in vision_result.attachment_statuses:
        attachment = images_by_id.get(status.attachment_id)
        if attachment is None:
            continue
        entry = dict(vision_attachments.get(attachment.id) or _vision_state_entry(config, attachment, status=status.status, mode=status.mode))
        entry.update(asdict(status))
        entry["kind"] = attachment.kind
        entry["size_bytes"] = attachment.size_bytes
        entry["resolved_path"] = _normalize_attachment_path(config, attachment)
        vision_attachments[attachment.id] = entry
        path_index[entry["resolved_path"]] = attachment.id

    status_ids = {status.attachment_id for status in vision_result.attachment_statuses}
    fallback_status = "unavailable" if vision_result.mode == "unavailable" else vision_result.mode
    for attachment in images_by_id.values():
        if attachment.id in status_ids:
            continue
        entry = dict(vision_attachments.get(attachment.id) or _vision_state_entry(config, attachment, status=fallback_status, mode=vision_result.mode))
        entry.update({"mode": vision_result.mode, "status": fallback_status, "errors": list(vision_result.errors)})
        vision_attachments[attachment.id] = entry
        path_index[entry["resolved_path"]] = attachment.id


def _vision_result_summary(vision_result: VisionPreprocessResult) -> tuple[int, int]:
    succeeded = sum(1 for status in vision_result.attachment_statuses if status.mode == "fallback" and status.status == "succeeded")
    failed = sum(1 for status in vision_result.attachment_statuses if status.status in {"failed", "unavailable"})
    return succeeded, failed


async def _emit_system_event(ctx: AgentRunContext, text: str) -> None:
    logger.info("vision preprocess status role=%s session_id=%s message=%s", ctx.role, ctx.session_id, text.strip())


def format_attachments_for_prompt(attachments: list[AttachmentRef], vision_result: VisionPreprocessResult | None = None) -> str:
    if not attachments:
        return ""

    consumed_ids = set(vision_result.consumed_attachment_ids) if vision_result else set()
    vision_mode = vision_result.mode if vision_result else "none"
    if consumed_ids and vision_mode == "fallback":
        handling_hint = (
            "图片附件已由辅助视觉模型识别，识别结果已在上文提供。"
            "不要再调用 inspect_image、read_file、browser_navigate 或其他工具读取这些图片；"
            "请直接基于辅助视觉模型输出和用户问题继续处理。"
        )
    elif consumed_ids and vision_mode == "direct":
        handling_hint = "图片附件已随本轮消息直接发送给主模型；不要再调用工具读取这些图片。"
    elif consumed_ids and vision_mode == "unavailable":
        handling_hint = (
            "系统已尝试处理图片但未能取得可用视觉内容。"
            "inspect_image、read_file、browser_navigate 不能替代视觉识别；不要调用这些工具假装读取图片内容。"
        )
    else:
        handling_hint = "请不要臆测附件内容；需要内容时调用文件或图片工具。不要在回复中泄露本地绝对路径。"

    lines = [
        "用户同时发送了以下附件：",
        handling_hint,
    ]
    for attachment in attachments:
        status = "已处理" if attachment.id in consumed_ids else "未处理"
        lines.extend(
            [
                f"- id: {attachment.id}",
                f"  status: {status}",
                f"  kind: {attachment.kind}",
                f"  filename: {attachment.filename or 'unknown'}",
                f"  content_type: {attachment.content_type or 'unknown'}",
                f"  size_bytes: {attachment.size_bytes if attachment.size_bytes is not None else 'unknown'}",
                f"  local_path: {attachment.local_path}",
            ]
        )
    return "\n".join(lines)


def message_with_attachments(message: str, attachments: list[AttachmentRef], vision_result: VisionPreprocessResult | None = None) -> str:
    attachment_block = format_attachments_for_prompt(attachments, vision_result)
    user_message = message.strip() or "用户发送了附件，请根据附件内容协助处理。"
    prefix = vision_result.text_prefix.strip() if vision_result and vision_result.text_prefix.strip() else ""
    parts = [part for part in [prefix, user_message, attachment_block] if part]
    if not parts:
        return message
    return "\n\n".join(parts)


def _with_direct_vision_parts(text: str, vision_result: VisionPreprocessResult | None) -> str | list[UserContent]:
    if not vision_result or not vision_result.direct_parts:
        return text
    return [text, *vision_result.direct_parts]


def _degrade_direct_vision_result(vision_result: VisionPreprocessResult, error: Exception) -> VisionPreprocessResult:
    """主模型直传图片失败后，降级为纯文本上下文，避免整轮对话崩溃。"""
    error_text = str(error).strip() or error.__class__.__name__
    text_prefix = render_system_prompt("vision/direct_error_prefix", {"error_text": error_text}) + "\n"
    return VisionPreprocessResult(
        mode="unavailable",
        text_prefix=text_prefix,
        consumed_attachment_ids=vision_result.consumed_attachment_ids,
        errors=[*vision_result.errors, error_text],
    )


@dataclass
class ExecutionOutcome:
    result: Any
    final_verification_signal: dict[str, object] | None = None


def _build_executor_message(
    task_frame_payload: dict[str, Any] | None,
    message: str,
    attachments: list[AttachmentRef] | None = None,
    vision_result: VisionPreprocessResult | None = None,
) -> str | list[UserContent]:
    """构造发送给 Executor 的 user-prompt。

    v2: 不再把 TaskFrame、时间块、记忆召回、SKILL 全文塞进 user-prompt。
    这些系统级运行时上下文统一由 ``factory._dynamic_system_prompt`` 通过
    ``@agent.system_prompt`` 钩子注入到 system prompt 末尾。user-prompt 只承载
    用户原话和附件元数据；对 Executor 单独再渲染 executor_task 模板，只是为了
    保留一个轻量"任务起点"提示。

    保留 ``task_frame_payload`` 入参是为了向后兼容（已有调用方仍会传），但内部
    不再嵌入 JSON。
    """
    del task_frame_payload  # noqa: F841 — kept for backward compatibility
    user_message = message_with_attachments(message, attachments or [], vision_result)
    text = render_system_prompt(
        "runtime/executor_task",
        {"user_message": user_message},
    )
    return _with_direct_vision_parts(text, vision_result)


def _build_repair_message(
    task_frame_payload: dict[str, Any] | None,
    message: str,
    verification_signal: dict[str, object],
    attachments: list[AttachmentRef] | None = None,
    vision_result: VisionPreprocessResult | None = None,
) -> str | list[UserContent]:
    del task_frame_payload  # noqa: F841 — kept for backward compatibility
    text = render_system_prompt(
        "runtime/executor_repair",
        {
            "verification_signal": json.dumps(verification_signal, ensure_ascii=False),
            "user_message": message_with_attachments(message, attachments or [], vision_result),
        },
    )
    return _with_direct_vision_parts(text, vision_result)


def _build_retry_message(
    task_frame_payload: dict[str, Any] | None,
    message: str,
    attachments: list[AttachmentRef] | None = None,
    vision_result: VisionPreprocessResult | None = None,
) -> str | list[UserContent]:
    del task_frame_payload  # noqa: F841 — TaskFrame 已在 system prompt 中注入
    text = render_system_prompt(
        "runtime/executor_retry",
        {
            "user_message": message_with_attachments(message, attachments or [], vision_result),
        },
    )
    return _with_direct_vision_parts(text, vision_result)


def _get_task_frame_payload(session_state: dict[str, Any]) -> dict[str, Any] | None:
    task_frame_payload = session_state.get("task_frame")
    if not isinstance(task_frame_payload, dict):
        return None
    payload = dict(task_frame_payload)
    known_paths = session_state.get("known_paths")
    if isinstance(known_paths, dict) and known_paths:
        payload["known_paths"] = known_paths
    return payload


def _build_executor_agent(config: AppConfig, role: str, task_frame_payload: dict[str, Any] | None, get_agent: Callable[[str, str], Any], run_mode: str = "interactive"):
    """选 executor agent 实例。

    渐进披露改造后,我们不再根据 ``task_frame.relevant_skills[*].allowed_tools``
    在轮内动态重建受限 executor —— skill 召回已经下放给主模型自己,通过
    ``get_skill_instructions`` 按需读全文,SKILL.md 里的 ``allowed-tools`` 字段
    不再作为 router 派生的硬性工具沙箱。``task_frame_payload`` 参数保留以兼容
    调用方签名,但不再被消费。
    """
    del task_frame_payload  # 渐进披露后不再用 relevant_skills 派生工具沙箱
    if run_mode == "scheduled":
        return build_scheduled_executor_agent(config, role)
    return get_agent(role, "executor")


async def run_executor_once(
    *,
    executor_agent: Any,
    run_message: str | list[UserContent],
    history: list[Any],
    deps: AgentDeps,
    config: AppConfig,
    stream: bool,
    handle_stream_events: Callable[[object, object], Any] | None,
) -> object:
    run_kwargs = {
        "message_history": history,
        "deps": deps,
        "usage_limits": UsageLimits(request_limit=60),
    }
    if stream and handle_stream_events is not None:
        run_kwargs["event_stream_handler"] = handle_stream_events
        return await executor_agent.run(run_message, **run_kwargs)

    return await asyncio.wait_for(
        executor_agent.run(run_message, **run_kwargs),
        timeout=config.agent_timeout_seconds,
    )


def preflight_compress_history(ctx: AgentRunContext) -> None:
    """轮内预检:history 粗略估计达硬上限时,做廉价压缩(无 LLM)防止 API 超限。

    针对单轮内 replan/repair/critique 多次 extend history 导致的膨胀。
    使用 allow_llm_summary=False 走确定性兜底,避免中途中断去调 LLM。
    """
    compressor = ctx.context_compressor
    if compressor is None:
        return
    try:
        if not compressor.should_compress_preflight(ctx.history):
            return
        before = len(ctx.history)
        compressed = compressor.compress(ctx.history, allow_llm_summary=False)
        if len(compressed) < before:
            ctx.history[:] = compressed
            logger.info(
                "context pre-flight compressed role=%s session_id=%s %d->%d",
                ctx.role,
                ctx.session_id,
                before,
                len(compressed),
            )
    except Exception:
        logger.warning(
            "pre-flight compress failed role=%s session_id=%s",
            ctx.role,
            ctx.session_id,
            exc_info=True,
        )


def _executor_stream_handler(ctx: AgentRunContext, text_buffer: list[str]) -> Callable[[object, object], Any] | None:
    # 正文实时流式：直接复用 turn.py 里构建的无缓冲 handler（ctx.stream_event_handler），
    # 不再传 text_buffer。原来传 text_buffer 会把所有正文 chunk 缓冲到 list 里，
    # 直到下游裁判跑完才在 _flush_buffered_text 一次性 flush，导致 WebUI
    # 看不到逐字打字机效果、整段回复在最后才一口气出现。
    #
    # 取消缓冲的代价：final_verification 要求重写时，首版正文已经
    # 流式显示给用户了，repair 的输出会作为续写追加在同一条 assistant 消息后，
    # 中间有 [System] 提示分隔。这与 ChatGPT/Claude 等主流产品的行为一致，
    # 优于"长时间静默后一次性吐出"。
    #
    # text_buffer 参数仅为兼容调用处保留，不再使用（_flush_buffered_text 退化为 no-op）。
    if not ctx.stream or ctx.stream_queue is None:
        return ctx.stream_event_handler
    return ctx.stream_event_handler


async def _flush_buffered_text(ctx: AgentRunContext, text_buffer: list[str]) -> None:
    # 正文已改为实时流式（见 _executor_stream_handler），此处保留为 no-op 兜底，
    # 不破坏 execute_task 既有的 repair/critique 分支结构。
    if not text_buffer or not ctx.stream or ctx.stream_queue is None:
        return
    for chunk in text_buffer:
        await ctx.stream_queue.put(StreamEventItem(type="text", text=chunk))
    text_buffer.clear()


async def _run_executor_with_vision_fallback(
    *,
    executor_agent: Any,
    task_frame_payload: dict[str, Any] | None,
    message: str,
    attachments: list[AttachmentRef],
    vision_result: VisionPreprocessResult,
    history: list[Any],
    deps: AgentDeps,
    config: AppConfig,
    stream: bool,
    handle_stream_events: Callable[[object, object], Any] | None,
) -> tuple[object, VisionPreprocessResult]:
    run_message = _build_executor_message(task_frame_payload, message, attachments, vision_result)
    try:
        result = await run_executor_once(
            executor_agent=executor_agent,
            run_message=run_message,
            history=history,
            deps=deps,
            config=config,
            stream=stream,
            handle_stream_events=handle_stream_events,
        )
        return result, vision_result
    except ModelHTTPError as exc:
        if not vision_result.direct_parts:
            raise
        logger.warning(
            "direct vision request failed; retrying without image parts model=%s status=%s",
            config.model_name,
            exc.status_code,
            exc_info=True,
        )
        mark_model_vision_support(config, False)
        degraded_vision_result = _degrade_direct_vision_result(vision_result, exc)
        degraded_message = _build_executor_message(task_frame_payload, message, attachments, degraded_vision_result)
        result = await run_executor_once(
            executor_agent=executor_agent,
            run_message=degraded_message,
            history=history,
            deps=deps,
            config=config,
            stream=stream,
            handle_stream_events=handle_stream_events,
        )
        return result, degraded_vision_result


async def _replan_after_execution_signal(
    ctx: AgentRunContext,
    signal: dict[str, object],
    get_agent: Callable[[str, str], Any],
) -> None:
    planner_agent = get_agent(ctx.role, "planner")
    if ctx.stream and ctx.stream_queue is not None:
        await ctx.stream_queue.put(StreamEventItem(type="system", text="\n\n[System] 执行遇到偏差，正在根据执行记录修订计划...\n", counted_as_output=False))
    planner_result = await planner_agent.run(
        render_system_prompt(
            "runtime/executor_replan",
            {
                "execution_ledger_signal": json.dumps(signal, ensure_ascii=False),
                "user_message": ctx.message,
            },
        ),
        message_history=ctx.history,
        deps=ctx.deps,
        event_stream_handler=ctx.stream_event_handler if ctx.stream else None,
    )
    ctx.history.extend(planner_result.all_messages())


async def execute_task(ctx: AgentRunContext, get_agent: Callable[[str, str], Any]) -> ExecutionOutcome:
    task_frame_payload = _get_task_frame_payload(ctx.session_state)
    # 易变上下文（时间/记忆/技能/TaskFrame）已通过 factory._dynamic_system_prompt
    # 注入到 system prompt 末尾，不再拼到 user-prompt 前缀。user-prompt 只承载
    # 用户原话和附件元数据，让 capture_turn_memories 拿到的就是干净的输入。
    if image_attachments(ctx.attachments):
        ctx.operation_state.start("vision", "preprocess")
        _mark_vision_processing(ctx.session_state, ctx.config, ctx.attachments)
        await _emit_system_event(ctx, "\n\n[System] 正在调用视觉模型识别图片附件，主模型将等待识别结果后再继续...\n")
    vision_result = await preprocess_vision_attachments(
        config=ctx.config,
        message=ctx.message,
        attachments=ctx.attachments,
    )
    _mark_vision_result(ctx.session_state, ctx.config, ctx.attachments, vision_result)
    succeeded, failed = _vision_result_summary(vision_result)
    if succeeded:
        await _emit_system_event(ctx, f"\n\n[System] 辅助视觉模型已完成 {succeeded} 张图片识别，后续将使用识别摘要并阻止重复读取同一图片。\n")
    elif failed or vision_result.errors:
        await _emit_system_event(ctx, "\n\n[System] 图片视觉识别未能取得可用结果，将按附件处理状态继续执行。\n")

    ctx.operation_state.start("model", "executor")
    executor_agent = _build_executor_agent(ctx.config, ctx.role, task_frame_payload, get_agent, run_mode=ctx.deps.run_mode)
    ledger_start_seq = get_ledger_length(ctx.session_state)
    ctx.session_state["current_turn_ledger_start_seq"] = ledger_start_seq
    # 清零本轮 final-answer validator 的连续打回计数与"yielded"旗标。
    # 这两个字段由 factory.py 的 _validate_execution_result 维护:counter 用于
    # VALIDATOR_HARD_LIMIT 硬熔断,yielded 旗标用于本函数末尾决定是否给用户加
    # "[System] 任务实际未完成"提示。跨轮必须清零,否则上一轮的死循环遗产会
    # 立刻让新一轮放行。
    ctx.session_state.pop("_final_validator_retries", None)
    ctx.session_state.pop("_final_validator_yielded", None)
    ctx.session_state.pop("_final_validator_last_signal", None)
    text_buffer: list[str] = []
    preflight_compress_history(ctx)

    try:
        result, vision_result = await _run_executor_with_vision_fallback(
            executor_agent=executor_agent,
            task_frame_payload=task_frame_payload,
            message=ctx.message,
            attachments=ctx.attachments,
            vision_result=vision_result,
            history=ctx.history,
            deps=ctx.deps,
            config=ctx.config,
            stream=ctx.stream,
            handle_stream_events=_executor_stream_handler(ctx, text_buffer),
        )
    except Exception as exc:
        signal = get_replan_signal(ctx.session_state, ledger_start_seq)
        # validator 反复打回最终会撞 UnexpectedModelBehavior(retries 耗尽)。
        # 这种情况下 ledger 里要么已经被 _validate_execution_result 写入了
        # <final_answer_validator> blocked 事件(满足 get_replan_signal 的"连续
        # >=2 次 blocked"条件),要么只发生过 1 次还不到阈值——此时仍然要救:
        # 用一个合成的 signal 强行触发 replan,让 planner 看到"答案被反复拦截"
        # 并据此修订计划(通常是补一步获取缺失信息、或把 blocked 任务拆细)。
        if (
            not signal
            and isinstance(exc, UnexpectedModelBehavior)
            and int(ctx.session_state.get("_final_validator_retries", 0) or 0) > 0
        ):
            signal = {
                "reason": "final answer validator retry exhausted",
                "consecutive_failures": int(ctx.session_state.get("_final_validator_retries", 0) or 0),
                "latest_status": "blocked",
                "events": [
                    {
                        "tool_name": "<final_answer_validator>",
                        "status": "blocked",
                        "detail": str(exc)[:500],
                    }
                ],
            }
        if signal and ctx.turn_state.replan_attempts < 1:
            ctx.turn_state.replan_attempts += 1
            text_buffer.clear()
            # 进入 replan 之前清掉 validator 旗标:replan 会重新跑一次 executor,
            # 那次的 validator 应该重新计数,而不是带着上次的"yielded"状态。
            ctx.session_state.pop("_final_validator_retries", None)
            ctx.session_state.pop("_final_validator_yielded", None)
            ctx.session_state.pop("_final_validator_last_signal", None)
            await _replan_after_execution_signal(ctx, signal, get_agent)
            preflight_compress_history(ctx)
            retry_message = _build_retry_message(task_frame_payload, ctx.message, ctx.attachments, vision_result)
            result = await run_executor_once(
                executor_agent=_build_executor_agent(ctx.config, ctx.role, task_frame_payload, get_agent, run_mode=ctx.deps.run_mode),
                run_message=retry_message,
                history=ctx.history,
                deps=ctx.deps,
                config=ctx.config,
                stream=ctx.stream,
                handle_stream_events=_executor_stream_handler(ctx, text_buffer),
            )
        else:
            raise

    # clarify_user 抛了 CallDeferred:run 已被 pydantic-ai 在 graph 层合法终止,
    # output 是 DeferredToolRequests 而非 final answer。下游 final_verification /
    # validator 等裁判全部跳过——任务在"等用户输入"这个合法暂停态,任何"补齐缺口"
    # 的尝试都会误伤(详见 tools/clarification.py 与 turn.py 的 deferred 分支处理)。
    if isinstance(getattr(result, "output", None), DeferredToolRequests):
        await _flush_buffered_text(ctx, text_buffer)
        logger.info(
            "execute_task: deferred tool request (clarify_user) — short-circuit "
            "role=%s session_id=%s",
            ctx.role,
            ctx.session_id,
        )
        return ExecutionOutcome(result=result)

    verification_signal = get_final_verification_signal(ctx.session_state)
    validator_yielded = bool(ctx.session_state.get("_final_validator_yielded", False))
    # validator 已经被硬熔断放行,说明模型已被反复证明"突破不了任务未完成的阻塞"
    # (常见原因:工具/权限缺失、用户输入不完整、模型坚持已完成)。再发起 repair
    # 轮次毫无意义——下一次 validator 看到 counter 仍在上限,会立刻再次 yield,
    # 浪费 LLM 调用并把模型反复拉进同一死局。直接把 signal 上抛给 turn.py,
    # 由后者走 suspend_current_plan + 给用户加 [System] 提示。
    if verification_signal and validator_yielded:
        await _flush_buffered_text(ctx, text_buffer)
        logger.info(
            "skipping final_verification_repair because validator was yielded "
            "(task is genuinely stuck, not a recoverable transient) role=%s session_id=%s",
            ctx.role,
            ctx.session_id,
        )
        return ExecutionOutcome(result=result, final_verification_signal=verification_signal)
    if verification_signal and ctx.turn_state.final_verification_repair_attempts < 1:
        ctx.turn_state.final_verification_repair_attempts += 1
        text_buffer.clear()
        if ctx.stream and ctx.stream_queue is not None:
            await ctx.stream_queue.put(StreamEventItem(type="system", text="\n\n[System] 最终验证发现任务尚未满足，正在补齐缺口...\n", counted_as_output=False))
        repair_message = _build_repair_message(task_frame_payload, ctx.message, verification_signal, ctx.attachments, vision_result)
        preflight_compress_history(ctx)
        result = await run_executor_once(
            executor_agent=_build_executor_agent(ctx.config, ctx.role, task_frame_payload, get_agent, run_mode=ctx.deps.run_mode),
            run_message=repair_message,
            history=ctx.history,
            deps=ctx.deps,
            config=ctx.config,
            stream=ctx.stream,
            handle_stream_events=_executor_stream_handler(ctx, text_buffer),
        )
        # repair 后模型可能又调 clarify_user 触发 deferred,这种情况也走短路。
        if isinstance(getattr(result, "output", None), DeferredToolRequests):
            await _flush_buffered_text(ctx, text_buffer)
            return ExecutionOutcome(result=result)
        verification_signal = get_final_verification_signal(ctx.session_state)

    if verification_signal:
        await _flush_buffered_text(ctx, text_buffer)
        return ExecutionOutcome(result=result, final_verification_signal=verification_signal)

    await _flush_buffered_text(ctx, text_buffer)
    return ExecutionOutcome(result=result)


def _find_pending_clarify_tool_call(history: list[Any]) -> str | None:
    """在历史末尾扫描未被 tool-return 消费的 clarify_user ToolCallPart。

    主要用于 session_state["_user_clarification"] 因为某些原因(进程重启、状态
    损坏)缺失 tool_call_id 时的兜底,以最大限度恢复 deferred 续接。
    """
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    consumed: set[str] = set()
    for msg in reversed(history):
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    consumed.add(part.tool_call_id)
        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if (
                    isinstance(part, ToolCallPart)
                    and part.tool_name == "clarify_user"
                    and part.tool_call_id not in consumed
                ):
                    return part.tool_call_id
            # 最近的 ModelResponse 没有 pending clarify → 不是一次 clarify resume
            break
    return None


async def execute_task_resume(
    ctx: AgentRunContext,
    get_agent: Callable[[str, str], Any],
) -> ExecutionOutcome | None:
    """处理 clarify_user 触发的下一轮:把用户回复以 deferred tool result 形式
    无感知地灌回模型。

    返回 None 表示无法 resume(状态损坏或 history 中找不到 pending tool call),
    上层应清理 ``_user_clarification`` 并退回正常 execute_task 路径。
    """
    info = ctx.session_state.get("_user_clarification", {}) or {}
    tool_call_id = info.get("tool_call_id") or _find_pending_clarify_tool_call(ctx.history)
    if not tool_call_id:
        logger.warning(
            "execute_task_resume: pending _user_clarification has no tool_call_id and "
            "history lacks an unconsumed clarify_user call; fall back to normal flow. "
            "role=%s session_id=%s",
            ctx.role,
            ctx.session_id,
        )
        ctx.session_state.pop("_user_clarification", None)
        return None

    # 恢复挂起的活动计划(若有):上一次 clarify_user 触发的 suspend_current_plan 把
    # todo_state.is_active 翻成了 False。回灌结果前先把它翻回去,模型回到工具循环
    # 之后才能正常 with_execution_guard。
    if has_restorable_suspended_plan(ctx.session_state):
        restore_suspended_plan(ctx.session_state, deps=ctx.deps)

    task_frame_payload = _get_task_frame_payload(ctx.session_state)
    ledger_start_seq = get_ledger_length(ctx.session_state)
    ctx.session_state["current_turn_ledger_start_seq"] = ledger_start_seq
    ctx.session_state.pop("_final_validator_retries", None)
    ctx.session_state.pop("_final_validator_yielded", None)
    ctx.session_state.pop("_final_validator_last_signal", None)

    results = DeferredToolResults(calls={tool_call_id: ctx.message})
    executor_agent = _build_executor_agent(
        ctx.config, ctx.role, task_frame_payload, get_agent, run_mode=ctx.deps.run_mode,
    )
    ctx.operation_state.start("model", "executor_resume")
    preflight_compress_history(ctx)
    text_buffer: list[str] = []

    run_kwargs: dict[str, Any] = {
        "message_history": ctx.history,
        "deferred_tool_results": results,
        "deps": ctx.deps,
        "usage_limits": UsageLimits(request_limit=60),
    }
    if ctx.stream and ctx.stream_event_handler is not None:
        run_kwargs["event_stream_handler"] = ctx.stream_event_handler

    logger.info(
        "execute_task_resume: feeding user reply as deferred tool result "
        "session_id=%s tool_call_id=%s reply_chars=%d",
        ctx.session_id,
        tool_call_id,
        len(ctx.message),
    )

    # 注意:run 的第一个位置参数(user_prompt)传 None —— 用户当前消息已经作为
    # tool return 注入,不再独立作 user-prompt;否则 graph 会拼一个空 ModelRequest
    # 让模型困惑。
    result = await executor_agent.run(None, **run_kwargs)

    # 消费成功 → 清掉 _user_clarification 标志(无论本轮是否又触发了新的 deferred)。
    ctx.session_state.pop("_user_clarification", None)

    # 模型在 resume 之后又调用了一次 clarify_user → 输出仍是 DeferredToolRequests,
    # 走和正常 execute_task 一致的短路语义。
    if isinstance(getattr(result, "output", None), DeferredToolRequests):
        await _flush_buffered_text(ctx, text_buffer)
        return ExecutionOutcome(result=result)

    verification_signal = get_final_verification_signal(ctx.session_state)
    if verification_signal:
        return ExecutionOutcome(result=result, final_verification_signal=verification_signal)
    return ExecutionOutcome(result=result)

