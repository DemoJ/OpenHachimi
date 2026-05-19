"""Executor orchestration with replan and final verification repair."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai.usage import UsageLimits

from openhachimi_agent.agent.execution import get_final_verification_signal, get_ledger_length, get_replan_signal
from openhachimi_agent.agent.factory import build_executor_agent
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.service.agent_runtime.context import AgentRunContext


logger = logging.getLogger(__name__)


@dataclass
class ExecutionOutcome:
    result: Any
    final_verification_signal: dict[str, object] | None = None


def _build_executor_message(task_frame_payload: dict[str, Any] | None, message: str) -> str:
    if not task_frame_payload:
        return message

    return (
        "请执行以下用户任务。必须遵守 TaskFrame 中的 goal、target_entities、invariants、allowed_autonomy 和 replan_triggers；"
        "如果工具观察结果与 TaskFrame 冲突，应停止当前动作并重新校准目标。\n"
        f"TaskFrame：{json.dumps(task_frame_payload, ensure_ascii=False)}\n"
        f"用户原始任务：{message}"
    )


def _build_repair_message(task_frame_payload: dict[str, Any] | None, message: str, verification_signal: dict[str, object]) -> str:
    return (
        "最终验证器发现当前执行结果尚不足以宣称完成。请只补齐验证器指出的缺口，"
        "继续严格遵守 TaskFrame、TODO 和执行记录；完成后必须更新 TODO 或提供足够证据。\n"
        f"TaskFrame：{json.dumps(task_frame_payload or {}, ensure_ascii=False)}\n"
        f"Final verification signal：{json.dumps(verification_signal, ensure_ascii=False)}\n"
        f"用户原始任务：{message}"
    )


def _build_retry_message(task_frame_payload: dict[str, Any] | None, message: str) -> str:
    return (
        "请根据刚刚修订后的计划继续执行用户任务。必须遵守 TaskFrame 和新的 TODO；"
        "如果再次遇到同类偏差，请停止并向用户说明阻塞原因。\n"
        f"TaskFrame：{json.dumps(task_frame_payload or {}, ensure_ascii=False)}\n"
        f"用户原始任务：{message}"
    )


def _get_task_frame_payload(session_state: dict[str, Any]) -> dict[str, Any] | None:
    task_frame_payload = session_state.get("task_frame")
    return task_frame_payload if isinstance(task_frame_payload, dict) else None


def _build_executor_agent(config: AppConfig, role: str, task_frame_payload: dict[str, Any] | None, get_agent: Callable[[str, str], Any]):
    executor_agent = get_agent(role, "executor")
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
        logger.info("sandboxing executor agent for role=%s restricted_tools=%s", role, allowed_tools_set)
        return build_executor_agent(config, role, allowed_tools=allowed_tools_set)
    return executor_agent


async def run_executor_once(
    *,
    executor_agent: Any,
    run_message: str,
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


async def _replan_after_execution_signal(
    ctx: AgentRunContext,
    signal: dict[str, object],
    get_agent: Callable[[str, str], Any],
) -> None:
    planner_agent = get_agent(ctx.role, "planner")
    if ctx.stream and ctx.stream_queue is not None:
        await ctx.stream_queue.put("\n\n[System] 执行遇到偏差，正在根据执行记录修订计划...\n")
    planner_result = await planner_agent.run(
        "Executor 在执行时触发了 TaskFrame 偏差或工具失败。请基于 TaskFrame、当前 TODO 和 execution ledger 摘要修订计划。\n"
        "要求：保持 TaskFrame 的 goal、target_entities、invariants 不变；不要扩大任务目标；"
        "如果原计划错误，请调用 create_todos 重建一个更窄、更可执行的计划。\n"
        f"TaskFrame：{json.dumps(_get_task_frame_payload(ctx.session_state) or {}, ensure_ascii=False)}\n"
        f"Execution ledger replan signal：{json.dumps(signal, ensure_ascii=False)}\n"
        f"用户原始任务：{ctx.message}",
        message_history=ctx.history,
        deps=ctx.deps,
    )
    ctx.history.extend(planner_result.all_messages())


async def execute_task(ctx: AgentRunContext, get_agent: Callable[[str, str], Any]) -> ExecutionOutcome:
    ctx.operation_state.start("model", "executor")
    task_frame_payload = _get_task_frame_payload(ctx.session_state)
    executor_agent = _build_executor_agent(ctx.config, ctx.role, task_frame_payload, get_agent)
    executor_message = _build_executor_message(task_frame_payload, ctx.message)
    ledger_start_seq = get_ledger_length(ctx.session_state)
    ctx.session_state["current_turn_ledger_start_seq"] = ledger_start_seq

    try:
        result = await run_executor_once(
            executor_agent=executor_agent,
            run_message=executor_message,
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
            retry_message = _build_retry_message(task_frame_payload, ctx.message)
            result = await run_executor_once(
                executor_agent=_build_executor_agent(ctx.config, ctx.role, task_frame_payload, get_agent),
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
            await ctx.stream_queue.put("\n\n[System] 最终验证发现任务尚未满足，正在补齐缺口...\n")
        repair_message = _build_repair_message(task_frame_payload, ctx.message, verification_signal)
        result = await run_executor_once(
            executor_agent=_build_executor_agent(ctx.config, ctx.role, task_frame_payload, get_agent),
            run_message=repair_message,
            history=ctx.history,
            deps=ctx.deps,
            config=ctx.config,
            stream=ctx.stream,
            handle_stream_events=ctx.stream_event_handler,
        )
        verification_signal = get_final_verification_signal(ctx.session_state)

    return ExecutionOutcome(result=result, final_verification_signal=verification_signal)
