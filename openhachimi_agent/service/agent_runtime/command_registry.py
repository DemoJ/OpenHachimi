"""跨渠道统一的命令注册表。

单一来源:所有渠道(CLI / Telegram / 微信 / HTTP)通过 `AgentService.dispatch_command`
共享同一份命令定义,包括别名识别、参数提示、帮助文案、Telegram 菜单。

每条命令由 `CommandSpec` 描述,handler 接收 `(service, args, role, session_id,
channel_context, channel)` 并返回 `CommandOutcome`。新增命令仅需在本文件
`_REGISTRY` 中追加一条 spec,无需改任何渠道代码。

注:与具体执行流相关的 `SIGNAL_LABELS` 等流式标签常量已迁移至 `streaming.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

from openhachimi_agent.core.identifiers import validate_latest_scope


if TYPE_CHECKING:
    from openhachimi_agent.service.agent_service import AgentService


OutcomeKind = Literal[
    "info",          # 普通信息回复
    "stop",          # 已中断当前任务
    "new_session",   # 切换到新 session
    "switch_role",   # 切换角色(同时换 session)
    "compress",      # 已压缩上下文
    "help",          # 返回帮助文案
    "exit",          # CLI 退出
    "start",         # Telegram /start
]


@dataclass
class CommandOutcome:
    """命令执行结果,供各渠道按需渲染。"""

    message: str
    kind: OutcomeKind = "info"
    role: str | None = None        # 不为 None 表示角色变更
    session_id: str | None = None  # 不为 None 表示会话变更


CommandHandler = Callable[
    ["AgentService", str, str | None, str | None, dict[str, object] | None, str],
    Awaitable[CommandOutcome],
]


@dataclass(frozen=True)
class CommandSpec:
    """命令元数据 + 执行函数。"""

    name: str
    aliases: tuple[str, ...]
    summary: str
    handler: CommandHandler
    args_hint: str = ""
    show_in_help: bool = True
    show_in_tg_menu: bool = True
    tg_menu_label: str = ""              # 留空则用 summary
    channels: frozenset[str] = field(default_factory=frozenset)  # 空 = 全渠道


# ────────────────────────────────────────────────────────────────────────────
# 解析与查找

_REGISTRY: list[CommandSpec] = []
_ALIAS_INDEX: dict[str, CommandSpec] = {}


def register(spec: CommandSpec) -> None:
    """注册一条命令(在模块底部默认注册,外部一般无需调用)。"""
    if any(existing.name == spec.name for existing in _REGISTRY):
        raise ValueError(f"command name conflict: {spec.name}")
    for alias in spec.aliases:
        if alias in _ALIAS_INDEX:
            raise ValueError(f"command alias conflict: {alias}")
    _REGISTRY.append(spec)
    for alias in spec.aliases:
        _ALIAS_INDEX[alias] = spec


def all_specs() -> tuple[CommandSpec, ...]:
    return tuple(_REGISTRY)


def parse_command(message: str) -> tuple[CommandSpec, str] | None:
    """识别消息首词是否命中命令别名;返回 (spec, args_text) 或 None。"""
    stripped = message.strip()
    if not stripped:
        return None
    # 单 token 命令(如 q / 退出)直接整串匹配
    if stripped in _ALIAS_INDEX:
        return _ALIAS_INDEX[stripped], ""
    # 首词命令:`/role default` → ("role", "default")
    head, _, rest = stripped.partition(" ")
    spec = _ALIAS_INDEX.get(head)
    if spec is None:
        return None
    return spec, rest.strip()


def iter_for_help(channel: str | None = None) -> list[CommandSpec]:
    return [
        spec
        for spec in _REGISTRY
        if spec.show_in_help and _channel_allowed(spec, channel)
    ]


def iter_for_tg_menu() -> list[CommandSpec]:
    return [
        spec
        for spec in _REGISTRY
        if spec.show_in_tg_menu and _channel_allowed(spec, "telegram")
    ]


def _channel_allowed(spec: CommandSpec, channel: str | None) -> bool:
    if not spec.channels:
        return True
    if channel is None:
        return False
    return channel in spec.channels


def build_help_text(channel: str | None = None) -> str:
    """渲染 /help 文案,按 channel 过滤(为空则展示全渠道命令)。"""
    lines = ["命令说明:"]
    for spec in iter_for_help(channel):
        # 首个别名作为代表展示;其余在括号里列出
        primary = spec.aliases[0]
        alt = [alias for alias in spec.aliases[1:]]
        alt_text = f"({'、'.join(alt)})" if alt else ""
        hint = f" {spec.args_hint}" if spec.args_hint else ""
        lines.append(f"  {primary}{hint}  {spec.summary}{alt_text}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# 默认 handler 实现
#
# handler 签名: async (service, args, role, session_id, channel_context, channel)
# 所有 handler 都从 channel_context 中复用 session_scope_key,与现有
# `_resolve_priority_session` 行为保持一致。

def _scope_from_context(channel_context: dict[str, object] | None) -> str | None:
    if not channel_context:
        return None
    raw = channel_context.get("session_scope_key")
    if not raw:
        return None
    return validate_latest_scope(str(raw))


async def _resolve_session(
    service: "AgentService",
    role: str | None,
    session_id: str | None,
    latest_scope: str | None,
) -> tuple[str, str]:
    """复用 service 的会话解析,返回 (规范化 role, 现有/最近 session_id)。"""
    return service._resolve_priority_session(role, session_id, latest_scope)  # noqa: SLF001


async def _handle_help(
    service: "AgentService",
    args: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None,
    channel: str,
) -> CommandOutcome:
    text = build_help_text(channel)
    return CommandOutcome(message=text, kind="help")


async def _handle_roles(
    service: "AgentService",
    args: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None,
    channel: str,
) -> CommandOutcome:
    resp = service.list_roles()
    current = service._normalize_role(role)  # noqa: SLF001
    lines = ["可用角色:"]
    for name in resp.roles:
        marker = "(当前)" if name == current else ""
        lines.append(f"  - {name}{marker}")
    return CommandOutcome(message="\n".join(lines), kind="info")


async def _handle_role(
    service: "AgentService",
    args: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None,
    channel: str,
) -> CommandOutcome:
    role_name = args.strip()
    if not role_name:
        return CommandOutcome(
            message="请在命令后跟上角色名,例如:/role default",
            kind="info",
        )
    latest_scope = _scope_from_context(channel_context)
    # 切换前先停掉旧 session(若有),与既有 CLI/Telegram 行为一致
    if session_id:
        try:
            await service.stop_session(session_id)
        except Exception:  # noqa: BLE001
            pass
    try:
        resp = service.switch_role(role_name, latest_scope=latest_scope)
    except (FileNotFoundError, ValueError) as exc:
        return CommandOutcome(message=f"切换角色失败:{exc}", kind="info")
    return CommandOutcome(
        message=resp.message,
        kind="switch_role",
        role=resp.role,
        session_id=resp.session_id,
    )


async def _handle_new(
    service: "AgentService",
    args: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None,
    channel: str,
) -> CommandOutcome:
    latest_scope = _scope_from_context(channel_context)
    resolved_role, resolved_session_id = await _resolve_session(service, role, session_id, latest_scope)
    if resolved_session_id:
        try:
            await service.stop_session(resolved_session_id)
        except Exception:  # noqa: BLE001
            pass
    resp = service.new_session(resolved_role, latest_scope=latest_scope)
    return CommandOutcome(
        message=resp.message,
        kind="new_session",
        role=resp.role,
        session_id=resp.session_id,
    )


async def _handle_stop(
    service: "AgentService",
    args: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None,
    channel: str,
) -> CommandOutcome:
    latest_scope = _scope_from_context(channel_context)
    resolved_role, resolved_session_id = await _resolve_session(service, role, session_id, latest_scope)
    resp = await service.stop_session(resolved_session_id)
    return CommandOutcome(
        message=resp.message,
        kind="stop",
        role=resolved_role,
        session_id=resolved_session_id,
    )


async def _handle_compress(
    service: "AgentService",
    args: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None,
    channel: str,
) -> CommandOutcome:
    latest_scope = _scope_from_context(channel_context)
    resolved_role, resolved_session_id = await _resolve_session(service, role, session_id, latest_scope)
    resp = await service.compress_session(resolved_role, resolved_session_id, args.strip(), latest_scope)
    return CommandOutcome(
        message=resp.output,
        kind="compress",
        role=resp.role,
        session_id=resp.session_id,
    )


async def _handle_exit(
    service: "AgentService",
    args: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None,
    channel: str,
) -> CommandOutcome:
    return CommandOutcome(message="已退出对话。", kind="exit")


async def _handle_start(
    service: "AgentService",
    args: str,
    role: str | None,
    session_id: str | None,
    channel_context: dict[str, object] | None,
    channel: str,
) -> CommandOutcome:
    """Telegram /start:新建会话 + 欢迎(具体欢迎文案由 telegram 渠道在 outcome 之外补充)。"""
    latest_scope = _scope_from_context(channel_context)
    resolved_role = service._normalize_role(role)  # noqa: SLF001
    resp = service.new_session(resolved_role, latest_scope=latest_scope)
    return CommandOutcome(
        message=resp.message,
        kind="start",
        role=resp.role,
        session_id=resp.session_id,
    )


# ────────────────────────────────────────────────────────────────────────────
# 默认命令清单

register(CommandSpec(
    name="help",
    aliases=("/help", "/帮助", "帮助"),
    summary="查看帮助信息",
    handler=_handle_help,
    show_in_tg_menu=False,  # Telegram 客户端本身在菜单显示帮助意义不大
))
register(CommandSpec(
    name="roles",
    aliases=("/roles", "/角色", "/list-roles"),
    summary="查看可用角色列表",
    handler=_handle_roles,
    tg_menu_label="🎭 查看可用角色列表",
))
register(CommandSpec(
    name="role",
    aliases=("/role", "/切换角色"),
    args_hint="<名称>",
    summary="切换角色(例:/role default)",
    handler=_handle_role,
    tg_menu_label="🔄 切换角色(如:/role default)",
))
register(CommandSpec(
    name="new",
    aliases=("/new", "/新对话", "新对话"),
    summary="保存当前对话,新建一段对话",
    handler=_handle_new,
    tg_menu_label="💾 保存当前对话,新建一段对话",
))
register(CommandSpec(
    name="stop",
    aliases=("/stop", "/停止", "停止"),
    summary="中断当前正在执行的任务",
    handler=_handle_stop,
    tg_menu_label="🛑 中断当前正在执行的任务",
))
register(CommandSpec(
    name="compress",
    aliases=("/compress", "/压缩"),
    args_hint="[主题]",
    summary="手动压缩上下文(可选焦点主题)",
    handler=_handle_compress,
    tg_menu_label="🗜️ 压缩当前对话上下文(可加主题)",
))
register(CommandSpec(
    name="exit",
    aliases=("/exit", "/quit", "/退出", "退出", "q"),
    summary="退出程序",
    handler=_handle_exit,
    show_in_tg_menu=False,
    channels=frozenset({"cli"}),
))
register(CommandSpec(
    name="start",
    aliases=("/start",),
    summary="开始使用,初始化会话",
    handler=_handle_start,
    show_in_help=False,
    show_in_tg_menu=False,  # Telegram 自动为新用户提供 /start
    channels=frozenset({"telegram"}),
))
