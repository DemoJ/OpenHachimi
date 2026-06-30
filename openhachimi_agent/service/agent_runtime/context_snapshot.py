"""executor 系统级上下文快照构建与 ModelRequest metadata 写入。

`AgentService` 持有 `_context_static_pool` / 工具目录缓存,这里提供接收 `service`
整体作参数的纯函数,负责:构建 executor 静态/动态 system prompt 段、计算静态
哈希、给本轮首个 UserPromptPart 的 ModelRequest 打上下文 metadata。读取侧由
``session_history`` 用同一组 metadata 键名还原展示用前缀。

与 ``context_cache`` 的关系:本模块是"写入侧"(构造快照 + 落 metadata),
``context_cache`` 是"缓存/池侧"(按 role+签名缓存工具目录、按 hash 查静态池)。
``context_cache`` 单向依赖本模块(``_extract_tool_catalog`` /
``_build_executor_static_context`` / ``_compute_static_hash``),本模块不反向
依赖,故无循环 import。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps


if TYPE_CHECKING:
    from openhachimi_agent.service.agent_service import AgentService


logger = logging.getLogger(__name__)


# WebUI 展示历史会话时需要"用户原始输入"，而 UserPromptPart 里实际只承载用户原话
# （v2 后已不再嵌 volatile 前缀），仍保留 metadata 旁路是为了：
# 1) 旧会话回放：旧版 UserPromptPart 里拼了 volatile 前缀，仅靠分隔符无法可靠反向
#    拆出原话，metadata 是稳妥的真值。
# 2) 兜底安全：万一未来又有路径往 user-prompt 塞了额外文本，metadata 仍能正确还原。
# 同时 stamp 两段"模型可见的 system 级上下文"快照,供 WebUI 在消息气泡的"运行时
# 上下文"折叠区展示。
#
# v3 改造（拆分静态/动态）：
# - 旧设计把整段 system prompt 文本（base.md + executor.md + role.md + config.md
#   + 工具清单 + 时间 + TaskFrame + 记忆 + 技能）原样塞进 metadata.openhachimi_system_context，
#   每条消息 5-15 KB,跨百轮膨胀到几 MB,且其中绝大多数是逐字相同的稳定段。
# - 新设计拆成两段持久化:
#     ``openhachimi_ctx_dynamic`` —— 每轮变的(时间/TaskFrame/记忆/命中技能),
#         由 build_system_dynamic_block(deps) 渲染,几百字到几 KB。
#     ``openhachimi_ctx_static_hash`` —— 稳定段(base/executor/role/config/tools)
#         的 SHA256[:16] 短哈希。完整文本写入 service 进程内 BoundedDict 池,
#         消息历史里只留 16 字符哈希。
#   读取时由 AgentService._resolve_static_context(role, hash) 从池中取出;池为空
#   时按 role 重建当前静态文本,哈希一致即回填池,不一致时降级只显示 dynamic。
# - 旧 key ``openhachimi_system_context`` 仍被读取作为旧会话回退;新路径不再写入。
# 读取侧键名常量(session_history 模块级,无下划线前缀)与本处写入侧一一对应。
_USER_MESSAGE_METADATA_KEY = "openhachimi_user_message"
_SYSTEM_CONTEXT_METADATA_KEY = "openhachimi_system_context"  # legacy, read-only
_CTX_DYNAMIC_METADATA_KEY = "openhachimi_ctx_dynamic"
_CTX_STATIC_HASH_METADATA_KEY = "openhachimi_ctx_static_hash"


def _stamp_turn_metadata(
    new_history: list,
    prev_len: int,
    user_message: str,
    dynamic_context: str,
    static_hash: str,
) -> None:
    """给本轮新增的、首个含 ``UserPromptPart`` 的 ``ModelRequest`` 打 metadata：

    - ``openhachimi_user_message``：用户原始输入（不含任何系统注入）。
    - ``openhachimi_ctx_dynamic``：本轮 system prompt 末尾的动态段(时间/
      TaskFrame/记忆/命中技能)。
    - ``openhachimi_ctx_static_hash``：本轮 executor 静态 system 段(base/
      executor/role/config/tools)的短哈希。完整文本在 ``AgentService._context_static_pool``
      内查表;读取时按需重建。

    Multi-step 单轮中可能多次往 history 追加 ``ModelRequest``（planner、
    executor_repair 等都会 extend），但首个 user 消息就是本轮入口。
    """
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    if not user_message:
        return
    payload: dict[str, str] = {_USER_MESSAGE_METADATA_KEY: user_message}
    if dynamic_context:
        payload[_CTX_DYNAMIC_METADATA_KEY] = dynamic_context
    if static_hash:
        payload[_CTX_STATIC_HASH_METADATA_KEY] = static_hash

    for idx in range(prev_len, len(new_history)):
        msg = new_history[idx]
        if not isinstance(msg, ModelRequest):
            continue
        if not any(isinstance(part, UserPromptPart) for part in getattr(msg, "parts", ())):
            continue
        meta = getattr(msg, "metadata", None)
        if meta is None:
            try:
                msg.metadata = dict(payload)
            except Exception:  # noqa: BLE001  # 极端情况 dataclass 被冻结时静默放弃
                logger.debug("failed to stamp turn metadata on ModelRequest idx=%d", idx)
            return
        # metadata 已存在：补齐我们这几项,不覆盖第三方已有键
        for k, v in payload.items():
            if k not in meta:
                meta[k] = v
        return


def _build_executor_static_context(
    config: AppConfig,
    role: str,
    executor_agent: object,
    service: "AgentService | None" = None,
) -> str:
    """直接构建 executor 的静态 system prompt 段(不含每轮动态注入的部分)。

    包括:base.md + agents/executor.md + role instructions + runtime/config.md
    + 可用工具摘要清单。每段独立 try/except,单段加载失败不影响其余。
    """
    from openhachimi_agent.content.prompts import load_system_prompt, render_system_prompt
    from openhachimi_agent.content.roles import load_role_content

    chunks: list[str] = []

    def append(fn, msg: str, *args: object) -> None:
        try:
            text = fn()
            if text:
                chunks.append(text)
        except Exception:
            logger.debug(msg, *args, exc_info=True)

    append(lambda: load_system_prompt("base"), "failed to load base.md")
    append(lambda: load_system_prompt("agents/executor"), "failed to load executor.md")
    append(lambda: load_role_content(config.roles_dir, role), "failed to load role content role=%s", role)
    append(
        lambda: render_system_prompt("runtime/config", {"user_dir": str(config.user_dir).replace("\\", "/")}),
        "failed to render config.md",
    )

    # 工具目录摘要 — 优先走 service 缓存
    if service is not None:
        append(lambda: service._get_cached_tool_catalog(role, executor_agent), "failed to get tool catalog from service")
    else:
        append(lambda: _extract_tool_catalog(executor_agent), "failed to extract tool catalog")

    return "\n\n".join(chunks)


def _extract_tool_catalog(executor_agent: object) -> str:
    """提取 executor agent 当前可用的工具清单(工具名 + 一行描述)。

    作为未命中 service 缓存的本地兜底。service 层有按 role+mcp_signature 缓存的
    版本;此函数留作 "no service" 容错路径。
    """
    try:
        toolsets = getattr(executor_agent, "_toolsets", None) or getattr(executor_agent, "toolsets", None)
        if not toolsets:
            return ""
        seen: set[str] = set()
        summary_lines: list[str] = []
        for toolset in toolsets:
            tools_attr = getattr(toolset, "tools", None)
            if tools_attr is None:
                continue
            if isinstance(tools_attr, dict):
                tool_iter = tools_attr.values()
            else:
                tool_iter = tools_attr
            for tool in tool_iter:
                name = getattr(tool, "name", None) or getattr(tool, "__name__", "") or ""
                if not name or name in seen:
                    continue
                seen.add(name)
                desc = getattr(tool, "description", "") or ""
                first_line = ""
                if desc.strip():
                    first_line = desc.strip().splitlines()[0].strip()
                else:
                    doc = getattr(tool, "__doc__", "") or ""
                    if doc.strip():
                        first_line = doc.strip().splitlines()[0].strip()
                if first_line:
                    summary_lines.append(f"- `{name}` — {first_line}")
                else:
                    summary_lines.append(f"- `{name}`")
        if not summary_lines:
            return ""
        return (
            "## 可用工具清单\n模型在本轮可以调用以下工具("
            "由 toolset 自动序列化为 OpenAI tool 协议发送):\n\n"
            + "\n".join(summary_lines)
        )
    except Exception:
        return ""


def _build_executor_dynamic_context(deps: AgentDeps | None) -> str:
    """构造 executor 本轮动态 system prompt 段(时间/TaskFrame/记忆/匹配技能 +
    executor 专用按需块)。

    主动调用 ``build_system_dynamic_block(deps)`` 和
    ``build_executor_extra_dynamic_block(deps)`` 生成,与 executor agent 的
    ``@agent.system_prompt`` 钩子拼装顺序保持一致(common 在前、executor 专用
    块在后),WebUI"展开运行时上下文"按钮才能看到与模型完全相同的快照。

    不再从历史消息中抽取,避免 multi-step turn 中取到 router 而非 executor
    system prompt 的问题。
    """
    from openhachimi_agent.content.runtime_context import (
        build_executor_extra_dynamic_block,
        build_system_dynamic_block,
    )

    parts: list[str] = []
    try:
        common = build_system_dynamic_block(deps)
        if common:
            parts.append(common)
    except Exception:  # noqa: BLE001
        logger.debug("build_system_dynamic_block failed", exc_info=True)
    try:
        executor_extra = build_executor_extra_dynamic_block(deps)
        if executor_extra:
            parts.append(executor_extra)
    except Exception:  # noqa: BLE001
        logger.debug("build_executor_extra_dynamic_block failed", exc_info=True)
    return "\n\n".join(parts)


def _compute_static_hash(text: str) -> str:
    """计算静态 system prompt 段的短内容哈希。

    使用 SHA256 前 16 字符,碰撞概率极低(2^64 空间),足够区分依赖变化。
    """
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _snapshot_executor_context(
    config: AppConfig,
    role: str,
    executor_agent: object,
    deps: AgentDeps,
    service: "AgentService | None" = None,
) -> tuple[str, str, str]:
    """构造本轮 executor 的 system 级上下文快照,返回 (static_text, dynamic_text, static_hash)。

    替代旧版 ``_build_full_system_context_snapshot``,优势:
    - 不再从历史消息反向抽取 SystemPromptPart(避免 multi-step turn 取到 router)
    - 静态/动态分离:静态段写入哈希池去重,动态段每轮单独持久化
    """
    static_text = _build_executor_static_context(config, role, executor_agent, service=service)
    dynamic_text = _build_executor_dynamic_context(deps)
    static_hash = _compute_static_hash(static_text)
    return static_text, dynamic_text, static_hash
