"""Planner agent orchestration."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from openhachimi_agent.agent.intent import TaskFrame
from openhachimi_agent.service.agent_runtime.context import AgentRunContext
from openhachimi_agent.service.agent_runtime.executor import message_with_attachments


PLANNING_NOTICE = "\n\n[System] 检测到任务需要前置规划，正在进行只读调研与计划拆解...\n"
PLANNING_HEARTBEAT = "\n[System] 规划仍在进行中，等待模型返回计划...\n"


def needs_planning(task_frame: TaskFrame) -> bool:
    return task_frame.requires_plan or task_frame.confidence < 0.5


def build_planner_prompt(task_frame: TaskFrame, message: str) -> str:
    return (
        "请针对以下 TaskFrame 制定执行计划。\n"
        "你只需要制定计划（使用 create_todos），不需要自己执行任何调研或搜索。\n"
        "Executor 拥有浏览器、文件操作、命令行、web_fetch、web_search 等全部工具，请基于对这些工具能力的理解来规划步骤。\n"
        "如果用户提供了明确的 URL 或文件路径，计划应从直接访问该目标开始。\n"
        "TaskFrame 是任务契约：计划必须继承 goal、target_entities、invariants，不得扩大或替换目标。\n"
        "计划中的每个任务应尽量包含 description、depends_on、success_criteria、verification、risk_level；"
        "如果某一步只允许特定工具，可填写 allowed_tools。\n"
        f"TaskFrame：{task_frame.model_dump_json(ensure_ascii=False)}\n"
        f"用户原始任务：{message}"
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

    heartbeat_task: asyncio.Task | None = None
    if ctx.stream:
        heartbeat_task = asyncio.create_task(_planner_heartbeat(ctx))

    try:
        planner_result = await planner_agent.run(
            build_planner_prompt(task_frame, message_with_attachments(ctx.message, ctx.attachments)),
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
