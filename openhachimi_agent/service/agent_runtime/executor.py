"""Executor orchestration with replan and final verification repair."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import UserContent
from pydantic_ai.usage import UsageLimits

from openhachimi_agent.agent.execution import get_final_verification_signal, get_ledger_length, get_replan_signal
from openhachimi_agent.agent.factory import build_executor_agent, build_scheduled_executor_agent
from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.service.agent_runtime.context import AgentRunContext
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
    user_message = message_with_attachments(message, attachments or [], vision_result)
    if not task_frame_payload:
        return _with_direct_vision_parts(user_message, vision_result)

    text = render_system_prompt(
        "runtime/executor_task",
        {
            "task_frame": json.dumps(task_frame_payload, ensure_ascii=False),
            "user_message": user_message,
        },
    )
    return _with_direct_vision_parts(text, vision_result)


def _build_repair_message(
    task_frame_payload: dict[str, Any] | None,
    message: str,
    verification_signal: dict[str, object],
    attachments: list[AttachmentRef] | None = None,
    vision_result: VisionPreprocessResult | None = None,
) -> str | list[UserContent]:
    text = render_system_prompt(
        "runtime/executor_repair",
        {
            "task_frame": json.dumps(task_frame_payload or {}, ensure_ascii=False),
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
    text = render_system_prompt(
        "runtime/executor_retry",
        {
            "task_frame": json.dumps(task_frame_payload or {}, ensure_ascii=False),
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
    scheduled_mode = run_mode == "scheduled"
    executor_agent = build_scheduled_executor_agent(config, role) if scheduled_mode else get_agent(role, "executor")
    if not task_frame_payload:
        return executor_agent

    relevant_skills = task_frame_payload.get("relevant_skills", [])
    if not relevant_skills:
        return executor_agent

    skills = find_skills(config.skills_dirs)
    allowed_tools_set: set[str] = set()
    is_restricted = False
    for skill in skills:
        if skill.config.name in relevant_skills and skill.config.allowed_tools:
            is_restricted = True
            allowed_tools_set.update(skill.config.allowed_tools)

    if is_restricted:
        logger.info("sandboxing executor agent for role=%s run_mode=%s restricted_tools=%s", role, run_mode, allowed_tools_set)
        if scheduled_mode:
            return build_scheduled_executor_agent(config, role, allowed_tools=allowed_tools_set)
        return build_executor_agent(config, role, allowed_tools=allowed_tools_set)
    return executor_agent


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
        await ctx.stream_queue.put(StreamEventItem(type="system", text="\n\n[System] 执行遇到偏差，正在根据执行记录修订计划...\n"))
    planner_result = await planner_agent.run(
        render_system_prompt(
            "runtime/executor_replan",
            {
                "task_frame": json.dumps(_get_task_frame_payload(ctx.session_state) or {}, ensure_ascii=False),
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
            handle_stream_events=ctx.stream_event_handler,
        )
    except Exception:
        signal = get_replan_signal(ctx.session_state, ledger_start_seq)
        if signal and ctx.turn_state.replan_attempts < 1:
            ctx.turn_state.replan_attempts += 1
            await _replan_after_execution_signal(ctx, signal, get_agent)
            retry_message = _build_retry_message(task_frame_payload, ctx.message, ctx.attachments, vision_result)
            result = await run_executor_once(
                executor_agent=_build_executor_agent(ctx.config, ctx.role, task_frame_payload, get_agent, run_mode=ctx.deps.run_mode),
                run_message=retry_message,
                history=ctx.history,
                deps=ctx.deps,
                config=ctx.config,
                stream=ctx.stream,
                handle_stream_events=ctx.stream_event_handler,
            )
        else:
            raise

    verification_signal = get_final_verification_signal(ctx.session_state)
    if verification_signal and ctx.turn_state.final_verification_repair_attempts < 1:
        ctx.turn_state.final_verification_repair_attempts += 1
        if ctx.stream and ctx.stream_queue is not None:
            await ctx.stream_queue.put(StreamEventItem(type="system", text="\n\n[System] 最终验证发现任务尚未满足，正在补齐缺口...\n"))
        repair_message = _build_repair_message(task_frame_payload, ctx.message, verification_signal, ctx.attachments, vision_result)
        result = await run_executor_once(
            executor_agent=_build_executor_agent(ctx.config, ctx.role, task_frame_payload, get_agent, run_mode=ctx.deps.run_mode),
            run_message=repair_message,
            history=ctx.history,
            deps=ctx.deps,
            config=ctx.config,
            stream=ctx.stream,
            handle_stream_events=ctx.stream_event_handler,
        )
        verification_signal = get_final_verification_signal(ctx.session_state)

    return ExecutionOutcome(result=result, final_verification_signal=verification_signal)
