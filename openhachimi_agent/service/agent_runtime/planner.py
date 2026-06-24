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
from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem


PLANNING_NOTICE = "\n\n[System] 检测到任务需要前置规划，正在进行只读调研与计划拆解...\n"
PLANNING_HEARTBEAT = "\n[System] 规划仍在进行中，等待模型返回计划...\n"


def needs_planning(task_frame: TaskFrame) -> bool:
    return task_frame.execution_mode == "planned" or task_frame.requires_plan


def build_planner_prompt(task_frame: TaskFrame, message: str) -> str:
    # TaskFrame 已通过 factory._dynamic_system_prompt 注入到 system prompt 末尾,
    # 这里 user-prompt 只承载用户原话,planner_task 模板再做轻量包装。
    del task_frame  # noqa: F841 — kept for backward compatibility with callers
    return render_system_prompt(
        "runtime/planner_task",
        {"user_message": message},
    )


async def _planner_heartbeat(ctx: AgentRunContext) -> None:
    if ctx.stream_queue is None:
        return
    interval = max(5, min(20, ctx.config.stream_idle_timeout_seconds // 2))
    while True:
        await asyncio.sleep(interval)
        await ctx.stream_queue.put(
            StreamEventItem(type="system", text=PLANNING_HEARTBEAT, counted_as_output=False),
        )


async def run_planner(ctx: AgentRunContext, task_frame: TaskFrame, get_agent: Callable[[str, str], Any]) -> object:
    planner_agent = get_agent(ctx.role, "planner")
    ctx.operation_state.start("planner", "initial_plan")
    if ctx.stream and ctx.stream_queue is not None:
        await ctx.stream_queue.put(StreamEventItem(type="system", text=PLANNING_NOTICE, counted_as_output=False))

    # 易变上下文（时间/记忆/技能/TaskFrame）已通过 factory._dynamic_system_prompt
    # 注入到 planner agent 的 system prompt 末尾，user-prompt 只承载用户原话+附件。
    user_content = message_with_attachments(ctx.message, ctx.attachments)

    heartbeat_task: asyncio.Task | None = None
    if ctx.stream:
        heartbeat_task = asyncio.create_task(_planner_heartbeat(ctx))

    try:
        # planner agent 的 ``output_type`` 是 ``ToolOutput(create_todos)``:
        # 模型 emit create_todos 即视为本次 run 的 final answer,graph 在工具执行
        # 后立即终止,不会再发起第 2 步 LLM 调用让模型 emit 一段重复的"执行步骤
        # 概览"自然语言。read_file / list_files 等只读调研工具仍可正常调用。
        # 这里复用 ctx.stream_event_handler(无文本过滤),工具事件直接透出。
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

    # ToolOutput 路径副作用补偿:create_todos 作为 output tool 时,pydantic-ai
    # graph 走的是"output schema 验证 + 执行"通道,**不发出** FunctionToolCallEvent。
    # 所以前面 stream_event_handler 收不到 create_todos 的工具卡片事件,UI 上
    # 看不到"✅ 创建计划:..."这一条。手动把它补回去——从 planner_result 末尾
    # 的 ModelResponse 中找到那个 ToolCallPart,合成一条与正常工具调用语义一致
    # 的卡片事件,塞进 stream queue。
    if ctx.stream and ctx.stream_queue is not None:
        _emit_output_tool_card(ctx, planner_result, tool_name="create_todos")

    # planner 故意不注册 clarify_user(见 ``tools/registry.py`` 的 PLANNER_TOOLSET
    # 注释),所以这里不会拿到 DeferredToolRequests 输出。下游 ``execute_task``
    # 直接接管即可。
    ctx.operation_state.start("model", "executor")
    ctx.history.extend(planner_result.all_messages())
    return planner_result


def _emit_output_tool_card(
    ctx: AgentRunContext,
    planner_result: object,
    *,
    tool_name: str,
) -> None:
    """从 planner_result 中提取指定 output tool 的 ToolCallPart,构造一条标准
    工具卡片事件塞进 stream_queue。

    用于补偿 pydantic-ai graph 对 output tool 不 emit FunctionToolCallEvent
    的副作用(让 UI 上还是能看到"✅ 创建计划:..."这一条)。
    """
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from openhachimi_agent.service.agent_runtime.streaming import (
        StreamEventItem,
        format_tool_call,
        tool_icon_for_name,
    )

    if ctx.stream_queue is None:
        return

    messages = []
    try:
        messages = list(planner_result.all_messages())  # type: ignore[attr-defined]
    except Exception:
        return

    # 最末一条 ModelResponse 是 planner 的最终输出(含 output tool 的 ToolCallPart)
    for msg in reversed(messages):
        if not isinstance(msg, ModelResponse):
            continue
        for part in msg.parts:
            if not isinstance(part, ToolCallPart):
                continue
            if part.tool_name != tool_name:
                continue
            try:
                args_dict = part.args_as_dict()
            except Exception:
                args_dict = {}
            card_text = format_tool_call(tool_name, args_dict)
            # 派发一次"工具调用"卡片事件——与正常 FunctionToolCallEvent 等价,
            # presenter 端按已有逻辑渲染(支持多行 detail / dedup 等)。
            try:
                ctx.stream_queue.put_nowait(
                    StreamEventItem(
                        type="tool",
                        text=card_text,
                        tool_name=tool_name,
                        tool_icon=tool_icon_for_name(tool_name),
                        temporary=False,
                        counted_as_output=False,
                    ),
                )
            except asyncio.QueueFull:  # pragma: no cover — queue 是 unbounded 设计
                pass
            return
        # 已找到最末一条 ModelResponse(无论是否含 target tool),不再继续往上找
        break
