"""单轮对话运行态构造:入口参数归一化、deps/记忆、AgentRunContext、MCP 幂等刷新。

提供 ``run_turn`` 在进入 per-session lock 前后用来构造 ``_TurnRunState`` 各字段的
纯函数:``_prepare_turn_inputs`` 归一化入口参数,``_build_turn_deps`` 构造 MemoryScope
与 AgentDeps(含子 agent 委派挂载点),``_build_run_context`` 构造 AgentRunContext 与
流式队列/统计,``_refresh_mcp_once`` 单轮内幂等刷新 MCP 配置。均接收 ``service`` 整体
作参数,无 service 状态字段持有。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.core.identifiers import validate_latest_scope
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.recall import recall_memories
from openhachimi_agent.service.agent_runtime.commands import channel_code_from_context
from openhachimi_agent.service.agent_runtime.context import AgentRunContext
from openhachimi_agent.service.agent_runtime.executor import message_with_attachments
from openhachimi_agent.service.agent_runtime.streaming import (
    StreamEventItem,
    StreamStats,
    build_stream_event_handler,
)
from openhachimi_agent.transport.api_models import AttachmentRef


if TYPE_CHECKING:
    from openhachimi_agent.service.agent_service import AgentService


logger = logging.getLogger(__name__)


def _prepare_turn_inputs(
    service: "AgentService",
    message: str,
    role: str | None,
    session_id: str | None,
    *,
    attachments: Sequence[AttachmentRef] | None,
    channel_context: dict[str, object] | None,
    scheduler_context: dict[str, object] | None,
    channel: str | None,
    delivery_target: dict[str, object] | None,
) -> SimpleNamespace:
    """归一化入口参数,解析 latest_scope / channel_code / effective_message / channel_context_data。"""
    role = service._normalize_role(role)
    session_id = service._normalize_session_id(session_id)
    service._validate_role_exists(role)
    latest_scope = (
        validate_latest_scope(str(channel_context.get("session_scope_key")))
        if channel_context and channel_context.get("session_scope_key")
        else None
    )
    # 渠道归属：从 channel_context 提取 channel_code（仅接受 CHANNEL_CODES 内的值），
    # 未识别时退到外层 channel 形参。后续传给 save_message_history 写 sidecar。
    resolved_channel_code = channel_code_from_context(channel_context)
    attachment_list = list(attachments or [])
    effective_message = message_with_attachments(message, attachment_list)

    channel_context_data = dict(channel_context or {})
    if not channel_context_data:
        channel_context_data = {"type": channel or "local", "platform": channel or "local"}
        if delivery_target:
            channel_context_data.update(delivery_target)
    channel_name = str(channel_context_data.get("type") or channel_context_data.get("platform") or "local")
    return SimpleNamespace(
        role=role,
        session_id=session_id,
        latest_scope=latest_scope,
        resolved_channel_code=resolved_channel_code,
        attachment_list=attachment_list,
        effective_message=effective_message,
        channel_context_data=channel_context_data,
        channel_name=channel_name,
        scheduler_context=dict(scheduler_context or {}),
    )


def _build_turn_deps(
    service: "AgentService",
    inputs: SimpleNamespace,
    session_state: dict[str, object],
    *,
    run_mode: str,
) -> tuple[AgentDeps, MemoryScope, object]:
    """构造 MemoryScope / recall / deps,并按 run_mode 注入子 agent 委派挂载点。"""
    role = inputs.role
    actual_session_id = inputs.session_id
    memory_scope = MemoryScope(
        tenant_id="local",
        user_id="local",
        role_name=role,
        session_id=actual_session_id,
        channel=inputs.channel_name,
    )
    memory_context = recall_memories(service.config, memory_scope, inputs.effective_message)
    session_state["memory_context"] = memory_context
    deps = AgentDeps(
        config=service.config,
        session_id=actual_session_id,
        browser_manager=service.browser_manager,
        process_manager=service.process_manager,
        session_state=session_state,
        memory_scope=memory_scope,
        memory_context=memory_context,
        session_store=service.session_store,
        run_mode=run_mode,
        role_name=role,
        channel_context=inputs.channel_context_data,
        scheduler_context=inputs.scheduler_context,
    )
    # 注入子 agent 委派挂载点(对齐 hermes delegate_task):subagent_agent 走 service._get_agent
    # 的 mtime 热重载缓存,subagent_registry 记录运行中子 agent task 供中断传播。失败降级 None
    # (delegate_task 返回不可用提示,不阻断主流程)。scheduled_executor 不注入(无人值守不委派)。
    if run_mode != "scheduled":
        try:
            from openhachimi_agent.agent.subagents import SubagentRegistry

            deps.subagent_agent = service._get_agent(role, "subagent")
            deps.subagent_registry = SubagentRegistry()
        except Exception:
            logger.debug("failed to inject subagent for role=%s", role, exc_info=True)
            deps.subagent_agent = None
            deps.subagent_registry = None
    return deps, memory_scope, memory_context


def _build_run_context(
    service: "AgentService",
    inputs: SimpleNamespace,
    history: list,
    deps: AgentDeps,
    session_state: dict[str, object],
    *,
    stream: bool,
    message: str,
) -> tuple[AgentRunContext, asyncio.Queue, StreamStats, dict[str, object]]:
    """构造 AgentRunContext + stream_queue + stream_stats + result_holder + compressor + event_handler。"""
    stream_queue: asyncio.Queue[StreamEventItem | object] = asyncio.Queue()
    stream_stats = StreamStats()
    result_holder: dict[str, object] = {}
    ctx = AgentRunContext(
        config=service.config,
        role=inputs.role,
        session_id=inputs.session_id,
        message=message,
        attachments=inputs.attachment_list,
        history=history,
        deps=deps,
        session_state=session_state,
        stream=stream,
        stream_queue=stream_queue,
    )
    ctx.stream_event_handler = build_stream_event_handler(stream_queue, ctx.operation_state)
    ctx.context_compressor = service._get_context_compressor(inputs.session_id)
    return ctx, stream_queue, stream_stats, result_holder


async def _refresh_mcp_once(
    service: "AgentService", ctx: AgentRunContext, deps: AgentDeps, refreshed: list[bool]
) -> None:
    """单轮内幂等刷新一次 MCP 配置。``refreshed`` 是单元素可变容器,短路第 2/3 次调用。

    MCP 配置在单轮内只刷新一次:router/planner/executor 三段编排原本各自调用一次,
    实际同一轮内文件 mtime 不会变,重复 stat + signature compare 是浪费。
    """
    if refreshed[0]:
        return
    refreshed[0] = True
    await service._maybe_reload_mcp_toolsets()
    ctx.config = service.config
    deps.config = service.config
