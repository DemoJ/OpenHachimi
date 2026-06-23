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
from openhachimi_agent.agent.intent import SelfCritiqueDecision
from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.service.agent_runtime.context import AgentRunContext, has_active_todos
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
    self_critique_signal: dict[str, object] | None = None


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


def _compact_text(value: object, max_chars: int = 1200) -> str:
    text = str(value or "")
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _summarize_todo_state(session_state: dict[str, Any]) -> list[dict[str, object]]:
    todo_state = session_state.get("todo_state")
    tasks = getattr(todo_state, "tasks", None)
    if not isinstance(tasks, dict):
        return []
    return [
        {
            "id": getattr(task, "id", task_id),
            "description": getattr(task, "description", ""),
            "status": getattr(task, "status", ""),
            "success_criteria": getattr(task, "success_criteria", ""),
            "notes": getattr(task, "notes", ""),
        }
        for task_id, task in list(tasks.items())[:20]
    ]


def _summarize_execution_evidence(session_state: dict[str, Any], max_events: int = 30) -> dict[str, object]:
    ledger = session_state.get("execution_ledger", [])
    turn_start_seq = int(session_state.get("current_turn_ledger_start_seq", 0) or 0)
    current_turn_events = []
    if isinstance(ledger, list):
        current_turn_events = [
            {
                "seq": event.get("seq"),
                "tool_name": event.get("tool_name"),
                "status": event.get("status"),
                "task_id": event.get("task_id"),
                "args": event.get("args", {}),
                "result_preview": _compact_text(event.get("result_preview", ""), 500),
                "violation": _compact_text(event.get("violation", ""), 500),
            }
            for event in ledger
            if isinstance(event, dict) and int(event.get("seq", 0)) > turn_start_seq
        ][-max_events:]

    artifacts = [
        {
            "id": getattr(artifact, "id", ""),
            "filename": getattr(artifact, "filename", ""),
            "content_type": getattr(artifact, "content_type", ""),
            "local_path": getattr(artifact, "local_path", ""),
        }
        for artifact in session_state.get("turn_artifacts", [])
    ]

    return {
        "todos": _summarize_todo_state(session_state),
        "current_turn_events": current_turn_events,
        "artifacts": artifacts,
        "known_paths": session_state.get("known_paths", {}),
    }


def _result_output(result: object) -> str:
    return str(getattr(result, "output", getattr(result, "data", "")) or "")


def _build_self_critique_message(
    task_frame_payload: dict[str, Any] | None,
    message: str,
    candidate_answer: str,
    execution_evidence: dict[str, object],
) -> str:
    return render_system_prompt(
        "runtime/self_critique_task",
        {
            "task_frame": json.dumps(task_frame_payload or {}, ensure_ascii=False),
            "user_message": message,
            "execution_evidence": json.dumps(execution_evidence, ensure_ascii=False),
            "candidate_answer": candidate_answer,
        },
    )


def _build_self_critique_repair_message(
    task_frame_payload: dict[str, Any] | None,
    message: str,
    candidate_answer: str,
    self_critique: SelfCritiqueDecision,
    execution_evidence: dict[str, object],
    attachments: list[AttachmentRef] | None = None,
    vision_result: VisionPreprocessResult | None = None,
) -> str | list[UserContent]:
    del task_frame_payload  # noqa: F841 — TaskFrame 已在 system prompt 中注入
    text = render_system_prompt(
        "runtime/executor_self_critique_repair",
        {
            "user_message": message_with_attachments(message, attachments or [], vision_result),
            "execution_evidence": json.dumps(execution_evidence, ensure_ascii=False),
            "candidate_answer": candidate_answer,
            "self_critique": self_critique.model_dump_json(),
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
    scheduled_mode = run_mode == "scheduled"
    executor_agent = build_scheduled_executor_agent(config, role) if scheduled_mode else get_agent(role, "executor")
    if not task_frame_payload:
        return executor_agent

    raw_skills = task_frame_payload.get("relevant_skills", [])
    if not raw_skills:
        return executor_agent

    # relevant_skills 在 model_dump(mode="json") 后是 list[dict]（新 SkillMatch
    # 结构），兼容历史 list[str] 路径以防早期持久化的 task_frame。
    skill_names: set[str] = set()
    for item in raw_skills:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name", "")).strip()
        else:
            name = str(getattr(item, "name", "") or "").strip()
        if name:
            skill_names.add(name)
    if not skill_names:
        return executor_agent

    skills = find_skills(config.skills_dirs)
    allowed_tools_set: set[str] = set()
    is_restricted = False
    for skill in skills:
        if skill.config.name in skill_names and skill.config.allowed_tools:
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
    # 直到 self_critique 跑完才在 _flush_buffered_text 一次性 flush，导致 WebUI
    # 看不到逐字打字机效果、整段回复在最后才一口气出现。
    #
    # 取消缓冲的代价：self_critique / final_verification 要求重写时，首版正文已经
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


def _self_critique_signal(decision: SelfCritiqueDecision) -> dict[str, object]:
    return {
        "reason": "self critique requires revision",
        "issues": [
            {
                "type": "self_critique_revision_required",
                "items": decision.issues,
                "repair_instructions": decision.repair_instructions,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
            }
        ],
    }


def _should_run_self_critique(ctx: AgentRunContext, task_frame_payload: dict[str, Any] | None) -> bool:
    """是否对本轮最终回复执行自检。

    自检会额外发起一次 LLM 调用并阻塞回复返回，对简单任务（问候、信息查询等）
    收益小、延迟代价大。因此只有「经历了复杂规划」的任务才自检：

    - 本轮有活动 TODO（实际创建并执行了多步计划）—— 最强信号；
    - TaskFrame 标记 requires_plan / execution_mode=planned / complexity=complex。

    简单 direct / skill_direct 任务直接返回，跳过自检。
    """
    if has_active_todos(ctx.session_state):
        return True
    if task_frame_payload:
        if task_frame_payload.get("requires_plan"):
            return True
        if task_frame_payload.get("execution_mode") == "planned":
            return True
        if task_frame_payload.get("complexity") == "complex":
            return True
    return False


async def _run_self_critique(
    ctx: AgentRunContext,
    get_agent: Callable[[str, str], Any],
    task_frame_payload: dict[str, Any] | None,
    result: object,
) -> SelfCritiqueDecision:
    candidate_answer = _result_output(result)
    evidence = _summarize_execution_evidence(ctx.session_state)
    prompt = _build_self_critique_message(task_frame_payload, ctx.message, candidate_answer, evidence)
    ctx.operation_state.start("model", "self_critique")
    critic_agent = get_agent(ctx.role, "self_critique")
    try:
        critic_result = await asyncio.wait_for(
            critic_agent.run(prompt),
            timeout=min(60, ctx.config.agent_timeout_seconds),
        )
        data = getattr(critic_result, "data", getattr(critic_result, "output", None))
        decision = SelfCritiqueDecision.model_validate(data)
    except Exception as exc:
        logger.warning("self critique failed; allowing executor result role=%s session_id=%s error=%s", ctx.role, ctx.session_id, exc)
        return SelfCritiqueDecision(
            verdict="pass",
            confidence=0.0,
            rationale=f"self critique failed: {exc.__class__.__name__}",
        )

    logger.info(
        "self critique verdict=%s confidence=%.2f issues=%d role=%s session_id=%s",
        decision.verdict,
        decision.confidence,
        len(decision.issues),
        ctx.role,
        ctx.session_id,
    )
    return decision


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
    except Exception:
        signal = get_replan_signal(ctx.session_state, ledger_start_seq)
        if signal and ctx.turn_state.replan_attempts < 1:
            ctx.turn_state.replan_attempts += 1
            text_buffer.clear()
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

    verification_signal = get_final_verification_signal(ctx.session_state)
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
        verification_signal = get_final_verification_signal(ctx.session_state)

    if verification_signal:
        await _flush_buffered_text(ctx, text_buffer)
        return ExecutionOutcome(result=result, final_verification_signal=verification_signal)

    # 简单任务（问候、信息查询等 direct/skill_direct）跳过自检，
    # 避免对低风险任务做额外 LLM 调用造成无谓延迟。只有经历了复杂规划
    # （活动 TODO / requires_plan / planned / complex）的任务才自检。
    if not _should_run_self_critique(ctx, task_frame_payload):
        await _flush_buffered_text(ctx, text_buffer)
        logger.info(
            "self critique skipped (simple task) role=%s session_id=%s",
            ctx.role,
            ctx.session_id,
        )
        return ExecutionOutcome(result=result)

    critique = await _run_self_critique(ctx, get_agent, task_frame_payload, result)
    if critique.verdict == "pass":
        await _flush_buffered_text(ctx, text_buffer)
        return ExecutionOutcome(result=result)

    if ctx.turn_state.self_critique_repair_attempts < 1:
        ctx.turn_state.self_critique_repair_attempts += 1
        text_buffer.clear()
        if ctx.stream and ctx.stream_queue is not None:
            await ctx.stream_queue.put(StreamEventItem(type="system", text="\n\n[System] 自检发现最终回复需要修正，正在补齐...\n", counted_as_output=False))

        evidence = _summarize_execution_evidence(ctx.session_state)
        repair_message = _build_self_critique_repair_message(
            task_frame_payload,
            ctx.message,
            _result_output(result),
            critique,
            evidence,
            ctx.attachments,
            vision_result,
        )
        ctx.operation_state.start("model", "self_critique_repair")
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
        verification_signal = get_final_verification_signal(ctx.session_state)
        if verification_signal:
            await _flush_buffered_text(ctx, text_buffer)
            return ExecutionOutcome(result=result, final_verification_signal=verification_signal)

        critique = await _run_self_critique(ctx, get_agent, task_frame_payload, result)
        if critique.verdict == "pass":
            await _flush_buffered_text(ctx, text_buffer)
            return ExecutionOutcome(result=result)

    await _flush_buffered_text(ctx, text_buffer)
    return ExecutionOutcome(result=result, self_critique_signal=_self_critique_signal(critique))
