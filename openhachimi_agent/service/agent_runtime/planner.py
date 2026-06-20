"""Planner agent orchestration."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from openhachimi_agent.content.prompts import render_system_prompt

from openhachimi_agent.agent.intent import TaskFrame
from openhachimi_agent.service.agent_runtime.context import AgentRunContext
from openhachimi_agent.service.agent_runtime.executor import message_with_attachments


PLANNING_NOTICE = "\n\n[System] 检测到任务需要前置规划，正在进行只读调研与计划拆解...\n"
PLANNING_HEARTBEAT = "\n[System] 规划仍在进行中，等待模型返回计划...\n"


def needs_planning(task_frame: TaskFrame) -> bool:
    return task_frame.execution_mode == "planned" or task_frame.requires_plan


def build_planner_prompt(task_frame: TaskFrame, message: str) -> str:
    return render_system_prompt(
        "runtime/planner_task",
        {
            "task_frame": task_frame.model_dump_json(ensure_ascii=False),
            "user_message": message,
        },
    )


async def _planner_heartbeat(ctx: AgentRunContext) -> None:
    if ctx.stream_queue is None:
        return
    interval = max(5, min(20, ctx.config.stream_idle_timeout_seconds // 2))
    while True:
        await asyncio.sleep(interval)
        await ctx.stream_queue.put(PLANNING_HEARTBEAT)


async def run_planner(ctx: AgentRunContext, task_frame: TaskFrame, get_agent: Callable[[str, str], Any]) -> object:
    planner_agent = get_agent(ctx.role, "planner")
    ctx.operation_state.start("planner", "initial_plan")
    if ctx.stream and ctx.stream_queue is not None:
        await ctx.stream_queue.put(PLANNING_NOTICE)

    # 易变上下文(时间/记忆/技能)注入用户消息前缀,保持系统提示稳定可缓存
    from openhachimi_agent.content.runtime_context import build_volatile_prefix

    volatile_prefix = build_volatile_prefix(ctx.deps)
    user_content = message_with_attachments(ctx.message, ctx.attachments)
    if volatile_prefix:
        user_content = f"{volatile_prefix}\n\n{user_content}"

    heartbeat_task: asyncio.Task | None = None
    if ctx.stream:
        heartbeat_task = asyncio.create_task(_planner_heartbeat(ctx))

    try:
        planner_result = await planner_agent.run(
            build_planner_prompt(task_frame, user_content),
            message_history=ctx.history,
            deps=ctx.deps,
            event_stream_handler=ctx.stream_event_handler if ctx.stream else None,
        )
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    ctx.operation_state.start("model", "executor")
    ctx.history.extend(planner_result.all_messages())
    return planner_result
