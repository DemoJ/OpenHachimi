"""主 agent 单次 run 编排(Hermes 式单循环)。

替代旧 ``executor.py`` 的 ``execute_task`` / ``execute_task_resume``。区别:

- **不再有 router/planner 前置分流** —— 主 agent 自主决定要不要建 todo。
- **不再有单 turn 内 replan/repair 串行** —— 失败由主 agent 自己 cancel+修订 todo;
  停止时的完整性校验由 ``factory._validate_execution_result`` 的 output_validator
  闸门(verification_stop + final_verification_signal)一次完成,不再二次拉起 planner。
- **保留** clarify_user 的 deferred 跨轮链路、视觉预处理、preflight 压缩、
  output_validator 闸门。

附件 / 视觉相关的辅助函数(``message_with_attachments`` 等)在 ``attachment_vision.py``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import UserContent
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults
from pydantic_ai.usage import UsageLimits

from openhachimi_agent.agent.execution import get_final_verification_signal, get_ledger_length
from openhachimi_agent.agent.verification_stop import reset_turn_verification
from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.memory.capture import REMEMBERED_THIS_TURN_KEY
from openhachimi_agent.service.agent_runtime.context import (
    AgentRunContext,
    has_active_todos,
    has_restorable_suspended_plan,
    restore_suspended_plan,
)
from openhachimi_agent.service.agent_runtime.attachment_vision import (
    _degrade_direct_vision_result,
    _mark_vision_processing,
    _mark_vision_result,
    _vision_result_summary,
    _with_direct_vision_parts,
    message_with_attachments,
)
from openhachimi_agent.service.agent_runtime.history_prep import prepare_history_for_run
from openhachimi_agent.transport.api_models import AttachmentRef
from openhachimi_agent.vision.capabilities import mark_model_vision_support
from openhachimi_agent.vision.preprocess import VisionPreprocessResult, image_attachments, preprocess_vision_attachments


logger = logging.getLogger(__name__)


# ── 视觉预处理状态提示 ──────────────────────────────────────────────────────


async def _emit_system_event(ctx: AgentRunContext, text: str) -> None:
    logger.info("vision preprocess status role=%s session_id=%s message=%s", ctx.role, ctx.session_id, text.strip())


# ── 主 agent run ────────────────────────────────────────────────────────────


@dataclass
class ExecutionOutcome:
    result: Any
    final_verification_signal: dict[str, object] | None = None


def _build_main_message(
    message: str,
    attachments: list[AttachmentRef] | None,
    vision_result: VisionPreprocessResult | None,
) -> str | list[UserContent]:
    """主 agent 的 user-prompt:executor_task 模板包裹用户原话 + 附件。"""
    user_message = message_with_attachments(message, attachments or [], vision_result)
    text = render_system_prompt(
        "runtime/executor_task",
        {"user_message": user_message},
    )
    return _with_direct_vision_parts(text, vision_result)


def _build_main_agent(role: str, run_mode: str, get_agent: Callable[..., Any]) -> Any:
    """选主 agent 实例(经 service._get_agent 统一缓存)。

    单一主 agent 持全套工具(含 create_todos / clarify_user / delegate_task);scheduled
    模式由 ``_build_base_agent`` 内部注入 scheduled_executor prompt 实现(build_main_agent
    透传 run_mode);scheduler 写工具的拦截由 scheduler 工具自身的
    ``ensure_scheduler_mutation_allowed`` 在 run_mode=scheduled 时负责;高危工具
    (命令执行/文件写入删除/技能安装)默认也在此模式下被 factory 裁剪,除非配置
    ``scheduler.security.allow_dangerous_tools_in_scheduled_runs`` 显式放开。
    """
    return get_agent(role, "main", run_mode=run_mode)


async def run_main_agent_once(
    *,
    main_agent: Any,
    run_message: str | list[UserContent],
    history: list[Any],
    deps: AgentDeps,
    config: AppConfig,
    stream: bool,
    handle_stream_events: Callable[[object, object], Any] | None,
) -> object:
    run_kwargs: dict[str, Any] = {
        "message_history": history,
        "deps": deps,
        "usage_limits": UsageLimits(request_limit=60),
    }
    if stream and handle_stream_events is not None:
        run_kwargs["event_stream_handler"] = handle_stream_events
        return await main_agent.run(run_message, **run_kwargs)

    if config.agent_timeout_seconds <= 0:
        return await main_agent.run(run_message, **run_kwargs)
    return await asyncio.wait_for(
        main_agent.run(run_message, **run_kwargs),
        timeout=config.agent_timeout_seconds,
    )


def preflight_compress_history(ctx: AgentRunContext) -> None:
    """轮内预检:history 粗略估计达硬上限时,做廉价压缩(无 LLM)防止 API 超限。

    针对单 turn 内主 agent 多步 ReAct 多次 extend history 导致的膨胀。使用
    ``allow_llm_summary=False`` 走确定性兜底,避免中途中断去调 LLM。
    """
    compressor = ctx.context_compressor
    if compressor is None:
        return
    try:
        if not compressor.should_compress_preflight(ctx.history):
            return
        before = len(ctx.history)
        result = compressor.compress(ctx.history, allow_llm_summary=False)
        if result.dropped:
            view = result.runtime_view  # type: ignore[attr-defined]
            if len(view) < before:
                ctx.history[:] = view
                logger.info(
                    "context pre-flight compressed role=%s session_id=%s %d->%d",
                    ctx.role,
                    ctx.session_id,
                    before,
                    len(view),
                )
    except Exception:
        logger.warning(
            "pre-flight compress failed role=%s session_id=%s",
            ctx.role,
            ctx.session_id,
            exc_info=True,
        )


def _main_stream_handler(ctx: AgentRunContext) -> Callable[[object, object], Any] | None:
    """正文实时流式:复用 ctx.stream_event_handler(无缓冲)。

    取消缓冲的代价:verification 闸门要求重写时,首版正文已经流式显示给用户,
    repair 输出会作为续写追加。这与 ChatGPT/Claude 等主流产品的行为一致,
    优于"长时间静默后一次性吐出"。
    """
    if not ctx.stream or ctx.stream_queue is None:
        return ctx.stream_event_handler
    return ctx.stream_event_handler


async def _run_main_with_vision_fallback(
    *,
    main_agent: Any,
    message: str,
    attachments: list[AttachmentRef],
    vision_result: VisionPreprocessResult,
    history: list[Any],
    deps: AgentDeps,
    config: AppConfig,
    stream: bool,
    handle_stream_events: Callable[[object, object], Any] | None,
) -> object:
    run_message = _build_main_message(message, attachments, vision_result)
    try:
        result = await run_main_agent_once(
            main_agent=main_agent,
            run_message=run_message,
            history=history,
            deps=deps,
            config=config,
            stream=stream,
            handle_stream_events=handle_stream_events,
        )
        return result
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
        degraded_message = _build_main_message(message, attachments, degraded_vision_result)
        result = await run_main_agent_once(
            main_agent=main_agent,
            run_message=degraded_message,
            history=history,
            deps=deps,
            config=config,
            stream=stream,
            handle_stream_events=handle_stream_events,
        )
        return result


def _is_transient_llm_error(exc: BaseException) -> bool:
    """模型调用层面的瞬时网络错误,值得整轮自动重试。

    覆盖:连接被对端中断(RemoteProtocolError,如网关 60s 流式超时掐断)、读写超时、
    连接失败、以及 5xx 网关/上游错误。4xx 客户端错误(参数问题/权限)不重试。
    """
    import httpx

    if isinstance(
        exc,
        (
            httpx.RemoteProtocolError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
        ),
    ):
        return True
    if isinstance(exc, ModelHTTPError):
        return bool(getattr(exc, "status_code", None) and exc.status_code >= 500)
    return False


async def _run_main_with_llm_retry(
    *,
    main_agent: Any,
    message: str,
    attachments: list[AttachmentRef],
    vision_result: VisionPreprocessResult,
    history: list[Any],
    deps: AgentDeps,
    config: AppConfig,
    stream: bool,
    handle_stream_events: Callable[[object, object], Any] | None,
) -> object:
    """对模型调用层面的瞬时网络错误做有限次自动重试(最多 2 次,指数退避)。

    长程任务中 LLM 网关偶发断连(如流式超时掐断 chunked 流)会把整轮打失败,
    用户得手动"继续"。这里把这类错误自动重试,重试成功则用户无感知。工具层错误、
    4xx、超时(走外层 asyncio.TimeoutError)不在此重试——它们重试无意义或会掩盖真因。
    """
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            return await _run_main_with_vision_fallback(
                main_agent=main_agent,
                message=message,
                attachments=attachments,
                vision_result=vision_result,
                history=history,
                deps=deps,
                config=config,
                stream=stream,
                handle_stream_events=handle_stream_events,
            )
        except Exception as exc:
            if attempt >= max_retries or not _is_transient_llm_error(exc):
                raise
            delay = 2**attempt
            logger.warning(
                "llm transient error, retrying %d/%d in %ds role=%s session_id=%s error=%s",
                attempt + 1, max_retries, delay, deps.role_name, deps.session_id, exc,
            )
            await asyncio.sleep(delay)
    # 理论上不可达(最后一次循环 raise),保险兜底。
    raise RuntimeError("llm retry loop exhausted unexpectedly")


async def _prepare_vision(ctx: AgentRunContext) -> VisionPreprocessResult:
    """图片附件的视觉预处理,并把识别状态写进 session_state、发系统事件。"""
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
    return vision_result


def _reset_turn_gates(ctx: AgentRunContext) -> None:
    """清零本轮 verification / final-answer validator 的计数与旗标,记下本轮 ledger 起点。

    跨轮必须清零,否则上一轮的死循环遗产会立刻让新一轮放行。
    """
    ctx.session_state["current_turn_ledger_start_seq"] = get_ledger_length(ctx.session_state)
    ctx.session_state.pop("_final_validator_retries", None)
    ctx.session_state.pop("_final_validator_yielded", None)
    ctx.session_state.pop("_final_validator_last_signal", None)
    ctx.session_state.pop(REMEMBERED_THIS_TURN_KEY, None)
    reset_turn_verification(ctx.session_state)


def _outcome_from_result(ctx: AgentRunContext, result: Any) -> ExecutionOutcome:
    """把一次 run 的结果包装成 ExecutionOutcome。

    clarify_user 抛了 CallDeferred 时,run 已被 pydantic-ai 在 graph 层合法终止,output 是
    DeferredToolRequests 而非最终回复;任务处于"等用户输入"的合法暂停态,跳过收尾校验。
    """
    if isinstance(getattr(result, "output", None), DeferredToolRequests):
        logger.info(
            "main agent: deferred tool request (clarify_user) — short-circuit role=%s session_id=%s",
            ctx.role,
            ctx.session_id,
        )
        return ExecutionOutcome(result=result)
    verification_signal = get_final_verification_signal(ctx.session_state)
    if verification_signal:
        return ExecutionOutcome(result=result, final_verification_signal=verification_signal)
    return ExecutionOutcome(result=result)


async def run_main_agent(ctx: AgentRunContext, get_agent: Callable[..., Any]) -> ExecutionOutcome:
    """主 agent 单次 run:视觉预处理 → 单次 agent.run → deferred 短路 → verification signal 上抛。

    停止时完整性校验由 ``factory._validate_execution_result`` 闸门一次完成。
    """
    vision_result = await _prepare_vision(ctx)
    ctx.operation_state.start("model", "main")
    main_agent = _build_main_agent(ctx.role, ctx.deps.run_mode, get_agent)
    _reset_turn_gates(ctx)
    preflight_compress_history(ctx)
    prepare_history_for_run(ctx.history)
    result = await _run_main_with_llm_retry(
        main_agent=main_agent,
        message=ctx.message,
        attachments=ctx.attachments,
        vision_result=vision_result,
        history=ctx.history,
        deps=ctx.deps,
        config=ctx.config,
        stream=ctx.stream,
        handle_stream_events=_main_stream_handler(ctx),
    )
    return _outcome_from_result(ctx, result)


# ── clarify_user deferred resume(从 executor.py 原样迁入,精简 plan 状态机依赖) ──


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
            break
    return None


_RESUME_PLAN_HINT = (
    "[系统提示] 以上是用户对你追问的回复。你还有一个进行中的计划:先确认这条回复补齐了哪一项,"
    "把它推进或标记完成,再继续剩下的 pending 项。"
)


def _resume_tool_call_id(ctx: AgentRunContext) -> str | None:
    """找到要回灌结果的 clarify_user 调用 id;找不到时清掉挂起标志并返回 None。"""
    info = ctx.session_state.get("_user_clarification", {}) or {}
    tool_call_id = info.get("tool_call_id") or _find_pending_clarify_tool_call(ctx.history)
    if not tool_call_id:
        logger.warning(
            "resume_main_agent: pending _user_clarification has no tool_call_id and "
            "history lacks an unconsumed clarify_user call; fall back to normal flow. "
            "role=%s session_id=%s",
            ctx.role,
            ctx.session_id,
        )
        ctx.session_state.pop("_user_clarification", None)
    return tool_call_id


def _resume_run_kwargs(ctx: AgentRunContext, tool_call_id: str) -> dict[str, Any]:
    """把用户回复作为 clarify_user 的结果灌回去的 run 参数。

    有进行中的计划时,把恢复提示拼在返回值里。不能另插一条系统消息:它会夹在工具调用
    和工具结果之间,部分 OpenAI 兼容服务会直接报错("assistant tool_calls 后必须紧跟 tool 消息")。
    """
    reply = ctx.message
    if has_active_todos(ctx.session_state):
        reply = f"{ctx.message}\n\n{_RESUME_PLAN_HINT}"
    run_kwargs: dict[str, Any] = {
        "message_history": ctx.history,
        "deferred_tool_results": DeferredToolResults(calls={tool_call_id: reply}),
        "deps": ctx.deps,
        "usage_limits": UsageLimits(request_limit=60),
    }
    if ctx.stream and ctx.stream_event_handler is not None:
        run_kwargs["event_stream_handler"] = ctx.stream_event_handler
    return run_kwargs


async def resume_main_agent(
    ctx: AgentRunContext,
    get_agent: Callable[..., Any],
) -> ExecutionOutcome | None:
    """处理 clarify_user 触发的下一轮:把用户回复以 deferred tool result 形式
    无感知地灌回主 agent。

    返回 None 表示无法 resume(状态损坏或 history 中找不到 pending tool call),
    上层应清理 ``_user_clarification`` 并退回正常 run_main_agent 路径。
    """
    tool_call_id = _resume_tool_call_id(ctx)
    if not tool_call_id:
        return None
    # 上一次 clarify_user 挂起了活动计划(todo_state.is_active=False),回灌前先恢复,
    # 模型回到工具循环之后才能正常推进。
    if has_restorable_suspended_plan(ctx.session_state):
        restore_suspended_plan(ctx.session_state, deps=ctx.deps)
    _reset_turn_gates(ctx)
    main_agent = _build_main_agent(ctx.role, ctx.deps.run_mode, get_agent)
    ctx.operation_state.start("model", "main_resume")
    preflight_compress_history(ctx)
    prepare_history_for_run(ctx.history)
    run_kwargs = _resume_run_kwargs(ctx, tool_call_id)
    logger.info(
        "resume_main_agent: feeding user reply as deferred tool result session_id=%s tool_call_id=%s reply_chars=%d",
        ctx.session_id,
        tool_call_id,
        len(ctx.message),
    )
    # run 的第一个位置参数(user_prompt)传 None:用户当前消息已经作为 tool return 注入,
    # 不再独立作 user-prompt;否则 graph 会拼一个空 ModelRequest 让模型困惑。
    result = await main_agent.run(None, **run_kwargs)
    # 消费成功 → 清掉 _user_clarification(无论本轮是否又触发了新的 deferred)
    ctx.session_state.pop("_user_clarification", None)
    return _outcome_from_result(ctx, result)
