"""优先命令分派。

`AgentService.dispatch_command` 是各渠道(CLI / HTTP / Telegram / 微信)统一的
斜杠命令入口。这里提供接收 `service` 整体作参数的纯函数,负责命令解析分派、
把命令结果包装为 ``ChatResponse`` / ``StreamEventItem``。`AgentService` 内对应
方法退化为薄壳。
"""

from __future__ import annotations

from openhachimi_agent.service.agent_runtime.command_registry import (
    CommandOutcome,
    parse_command,
)
from openhachimi_agent.service.agent_runtime.commands import latest_scope_from_context
from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem
from openhachimi_agent.transport.api_models import ChatResponse


def resolve_priority_session(
    service,
    role: str | None,
    session_id: str | None,
    latest_scope: str | None = None,
) -> tuple[str, str]:
    resolved_role = service._normalize_role(role)
    resolved_session_id = service._normalize_session_id(session_id)
    if resolved_session_id:
        return resolved_role, resolved_session_id
    latest = service.latest_session(resolved_role, latest_scope=latest_scope)
    return latest.role, latest.session_id


async def dispatch_command(
    service,
    message: str,
    *,
    role: str | None = None,
    session_id: str | None = None,
    channel_context: dict[str, object] | None = None,
    channel: str = "local",
) -> CommandOutcome | None:
    """统一命令分派入口:命中注册表则执行,未命中或不可用于该渠道返回 None。"""
    parsed = parse_command(message)
    if parsed is None:
        return None
    spec, args = parsed
    if spec.channels and channel not in spec.channels:
        return None
    return await spec.handler(service, args, role, session_id, channel_context, channel)


async def handle_priority_command_response(
    service,
    message: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None = None,
    channel: str = "local",
) -> ChatResponse | None:
    outcome = await dispatch_command(
        service,
        message,
        role=role,
        session_id=session_id,
        channel_context=channel_context,
        channel=channel,
    )
    if outcome is None:
        return None
    # send_message/stream_events 这条路径只需把命令结果包装为 ChatResponse
    # (kind=exit 等不会从这里进:HTTP/微信 渠道在更外层已经拦截)
    latest_scope = latest_scope_from_context(channel_context)
    resolved_role, resolved_session_id = resolve_priority_session(service, role, session_id, latest_scope)
    return ChatResponse(
        output=outcome.message,
        role=outcome.role or resolved_role,
        session_id=outcome.session_id or resolved_session_id,
    )


async def handle_priority_command_events(
    service,
    message: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None = None,
    channel: str = "local",
) -> list[StreamEventItem] | None:
    response = await handle_priority_command_response(
        service, message, role, session_id, channel_context, channel=channel,
    )
    if response is None:
        return None
    # 斜杠命令的输出本身是要给用户看的正文,走 type="text"。
    # type="system" 现在专表"运行时状态提示",会在 stream_events 出口处被统一过滤掉。
    return [StreamEventItem(type="text", text=response.output)]
