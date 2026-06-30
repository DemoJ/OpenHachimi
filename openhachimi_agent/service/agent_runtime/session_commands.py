"""角色与会话身份命令。

`AgentService` 持有 `config` / `session_store` 等字段,这里提供接收 `service`
整体作参数的纯函数,实现角色规范化、存在性校验、以及 ``latest``/``new``/``switch``
等会话命令。`AgentService` 内对应方法退化为薄壳,签名与字段名保持不变。
"""

from __future__ import annotations

import logging

from openhachimi_agent.content.roles import list_role_names, load_role_content
from openhachimi_agent.core.identifiers import (
    validate_latest_scope,
    validate_role_name,
    validate_session_id,
)
from openhachimi_agent.transport.api_models import CommandResponse, RolesResponse


logger = logging.getLogger(__name__)


def normalize_role(service, role_name: str | None) -> str:
    return validate_role_name(role_name or service.config.default_role_name)


def normalize_session_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    return validate_session_id(session_id, allow_legacy=False)


def validate_role_exists(service, role_name: str) -> None:
    if role_name == service.config.default_role_name and not list_role_names(service.config.roles_dir):
        return
    load_role_content(service.config.roles_dir, role_name)


def state(service):
    from openhachimi_agent.transport.api_models import AgentState

    return AgentState(
        model=service.config.model_name,
        base_url=service.config.openai_base_url or None,
        mcp_servers=len(service._mcp_toolsets),
        mcp_errors=list(service._mcp_errors),
    )


def list_roles(service) -> RolesResponse:
    logger.debug("listing roles roles_dir=%s", service.config.roles_dir)
    return RolesResponse(
        roles=list_role_names(service.config.roles_dir),
        current_role=service.config.default_role_name,
    )


def latest_session(service, role_name: str | None = None, latest_scope: str | None = None) -> CommandResponse:
    role = normalize_role(service, role_name)
    validate_role_exists(service, role)
    scope = validate_latest_scope(latest_scope)
    session_id = service.session_store.get_latest_session_id(role, scope)
    if not session_id:
        session_id = service.session_store.new_session_id()
        service.session_store.set_latest_session_id(role, session_id, scope)
        logger.info("no latest session found, created new session role=%s session_id=%s scope=%s", role, session_id, scope)
    else:
        logger.info("loaded latest session role=%s session_id=%s scope=%s", role, session_id, scope)

    return CommandResponse(
        message="已恢复上一次的对话上下文。",
        role=role,
        session_id=session_id,
    )


def new_session(service, role_name: str | None = None, latest_scope: str | None = None) -> CommandResponse:
    role = normalize_role(service, role_name)
    validate_role_exists(service, role)
    scope = validate_latest_scope(latest_scope)
    session_id = service.session_store.start_new_session(role, scope)
    logger.info(
        "new session role=%s session_id=%s scope=%s",
        role,
        session_id,
        scope,
    )
    lines = [
        "✨ 新对话已准备好",
        "",
        "✅ 上一段对话已保存",
        "📝 已为你开启一段全新的上下文",
        "",
        "━━ 当前配置 ━━",
        f"🤖 模型:{service.config.model_name}",
    ]
    if service.config.openai_base_url:
        lines.append(f"🌐 模型服务:{service.config.openai_base_url}")
    lines.extend([
        f"🎭 角色:{role}",
        f"🧩 会话:{session_id}",
        "",
        "💬 直接输入内容并回车,即可继续对话。",
    ])
    return CommandResponse(
        message="\n".join(lines),
        role=role,
        session_id=session_id,
    )


def switch_role(service, role_name: str, latest_scope: str | None = None) -> CommandResponse:
    role = normalize_role(service, role_name)
    validate_role_exists(service, role)
    scope = validate_latest_scope(latest_scope)
    session_id = service.session_store.start_new_session(role, scope)
    logger.info(
        "switched role to role=%s session_id=%s scope=%s",
        role,
        session_id,
        scope,
    )
    return CommandResponse(
        message=f"已切换到角色:{role},并新建对话。",
        role=role,
        session_id=session_id,
    )


def new_session_for_channel(
    service,
    role_name: str | None,
    channel_code: str,
    *,
    latest_scope: str | None = None,
) -> CommandResponse:
    """为指定渠道新建会话并立即在 SessionStore 写入渠道归属。

    WebUI 在用户没有选中会话直接发消息时(空白页自动 /new)调用此方法,
    保证新会话从一开始就有渠道标签,不会落到 ``DEFAULT_CHANNEL`` 默认值。
    ``latest_scope`` 未传时默认用 ``channel_code`` 自身作为 scope —— 这
    与 HTTP /chat/stream 的 ``session_scope_key`` 行为对齐。
    """
    from openhachimi_agent.storage.session_store import CHANNEL_CODES, DEFAULT_CHANNEL

    role = normalize_role(service, role_name)
    validate_role_exists(service, role)
    if channel_code not in CHANNEL_CODES:
        channel_code = DEFAULT_CHANNEL
    scope = validate_latest_scope(latest_scope or channel_code)
    # start_new_session 内部:写 sessions(channel 首写定终身)+ 写 pointer,一并完成。
    session_id = service.session_store.start_new_session(
        role, scope, channel=channel_code, scope_key=scope,
    )
    logger.info(
        "new session for channel role=%s session_id=%s channel=%s scope=%s",
        role, session_id, channel_code, scope,
    )
    return CommandResponse(
        message="已新建会话。",
        role=role,
        session_id=session_id,
    )
