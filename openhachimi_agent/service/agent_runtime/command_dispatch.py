"""优先命令分派。

`AgentService.dispatch_command` 是各渠道(CLI / HTTP / Telegram / 微信)统一的
斜杠命令入口。这里提供接收 `service` 整体作参数的纯函数,负责命令解析分派、
把命令结果包装为 ``ChatResponse`` / ``StreamEventItem``。`AgentService` 内对应
方法退化为薄壳。
"""

from __future__ import annotations

import difflib

from openhachimi_agent.service.agent_runtime.command_registry import (
    CommandOutcome,
    all_specs,
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
    """统一命令分派入口:命中注册表则执行,未命中或不可用于该渠道返回 None。

    例外:以 ``/`` 开头但未命中任何命令的消息,返回"未知命令"提示而不是 None——
    此前这类消息(如拼错的 /helpp)会被当普通聊天发给 LLM,用户得到模型困惑的
    回复甚至被模型执行了命令语义。
    """
    parsed = parse_command(message)
    if parsed is None:
        stripped = message.strip()
        if stripped.startswith("/"):
            return _unknown_command_outcome(stripped, channel)
        return None
    spec, args = parsed
    if spec.channels and channel not in spec.channels:
        return None
    return await spec.handler(service, args, role, session_id, channel_context, channel)


def _unknown_command_outcome(message: str, channel: str) -> CommandOutcome:
    """生成"未知命令 + 相近命令建议"提示。"""
    head = message.split(" ", 1)[0]
    known: list[str] = []
    for spec in all_specs():
        if spec.channels and channel not in spec.channels:
            continue
        known.extend(alias for alias in spec.aliases if alias.startswith("/"))
    head_key = head.lstrip("/")
    suggestions = difflib.get_close_matches(head_key, [a.lstrip("/") for a in known], n=2, cutoff=0.5)
    suggestion_text = ""
    if suggestions:
        suggestion_text = f"你是不是想输入：{'、'.join('/' + s for s in suggestions)}？\n"
    available = "、".join(sorted(set(known)))
    return CommandOutcome(
        message=(
            f"未知命令：{head}。\n"
            f"{suggestion_text}"
            f"可用命令：{available}\n"
            "输入 /help 查看说明；如想把这当作普通消息发送，请去掉开头的 /。"
        ),
        kind="error",
    )


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
    # 斜杠命令的输出本身是要给用户看的正文,走 type="text"。
    # system 专表"运行时状态提示"(planner heartbeat 等),在 stream_events 出口过滤;
    # 面向用户的提示统一走 notice。
    events: list[StreamEventItem] = [StreamEventItem(type="text", text=outcome.message)]

    # 命令改变了会话/角色(如 /new、/role、/back):把最新指向回传给前端。
    # 此前 outcome 的变更信息在这里被丢弃,WebUI 的 currentSessionId/currentRole
    # 不更新,用户下一条消息仍发往旧会话旧角色。
    latest_scope = latest_scope_from_context(channel_context)
    resolved_role, resolved_session_id = resolve_priority_session(service, role, session_id, latest_scope)
    final_role = outcome.role or resolved_role
    final_session_id = outcome.session_id or resolved_session_id
    role_changed = bool(outcome.role) and outcome.role != role
    session_changed = bool(outcome.session_id) and outcome.session_id != session_id
    if role_changed or session_changed:
        events.append(
            StreamEventItem(
                type="session",
                text="",
                session_id=final_session_id,
                role=final_role,
                counted_as_output=False,
            )
        )
    return events
