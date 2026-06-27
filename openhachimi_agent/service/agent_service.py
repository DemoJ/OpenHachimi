"""Agent 后台服务层。"""

import asyncio
import contextlib
import logging
import time
import weakref
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

from openhachimi_agent.content.roles import list_role_names, load_role_content
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.identifiers import validate_latest_scope, validate_role_name, validate_session_id
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.service.agent_runtime.agent_cache import (
    AGENT_DEPENDENCY_MTIME_TTL_SECONDS,
    compute_dependency_mtime,
    get_or_build_agent,
)
from openhachimi_agent.service.agent_runtime.command_registry import (
    CommandOutcome,
    parse_command,
)
from openhachimi_agent.service.agent_runtime.commands import (
    channel_code_from_context,
    latest_scope_from_context,
)
from openhachimi_agent.service.agent_runtime.mcp_manager import (
    get_mcp_config_signature,
    load_new_mcp_stack,
)
from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem
from openhachimi_agent.service.agent_runtime.turn import run_turn
from openhachimi_agent.transport.api_models import (
    AgentState,
    ArtifactRef,
    AttachmentRef,
    ChatResponse,
    CommandResponse,
    RolesResponse,
)


logger = logging.getLogger(__name__)


class AgentService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._agents: dict[str, tuple[Any, float]] = {}  # 缓存 (Agent 实例, 最后修改时间),支持热重载
        self._agent_dependency_mtime_cache: tuple[float, float] | None = None
        self._mcp_toolsets: list = []
        self._mcp_stack = contextlib.AsyncExitStack()
        self._mcp_config_signature: tuple[float, int] | None = None
        self._mcp_reload_lock = asyncio.Lock()
        self._mcp_errors: list[str] = []
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        from openhachimi_agent.service.browser import BrowserManager
        from openhachimi_agent.service.process import ProcessManager
        from openhachimi_agent.storage.session_store import SessionStore
        from openhachimi_agent.tools.utils import BoundedDict
        self.browser_manager = BrowserManager(config)
        self.process_manager = ProcessManager()
        # 会话级 SQLite 库:消息历史 + 渠道元数据 + 最新指针 + TODO state。
        # 取代旧的 .memory/{role}/*.json + *.meta.json + latest* + todos/*.json 文件方案。
        self.session_store = SessionStore(config.memory_dir / "sessions.sqlite3")
        self._session_states: BoundedDict[str, dict] = BoundedDict(100)
        self._artifact_records: BoundedDict[str, ArtifactRef] = BoundedDict(500)
        self._context_compressors: BoundedDict[str, Any] = BoundedDict(100)
        # 进程内静态 system prompt 池:hash -> 文本。turn.py 写入,_extract_text_parts 读取。
        # 大小上限 200 已足够覆盖 (角色数 * 工具集变种 + 滞留),命中失败时按需回填。
        self._context_static_pool: BoundedDict[str, str] = BoundedDict(200)
        # 工具目录摘要按 role 缓存:key=f"{role}:executor",value=(text, signature)。
        # signature 由 mcp_config_signature 和 agent_dependency_mtime_cache 组成,二者
        # 均在内部状态发生变化时由相关路径主动更新。
        self._tool_catalog_cache: dict[str, tuple[str, tuple[Any, Any]]] = {}
        logger.info(
            "service initialized model=%s",
            self.config.model_name,
        )

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """启动后台服务需要长期维护的资源,如 MCP 连接。"""
        await self._reload_mcp_toolsets(force=True)

    async def stop(self) -> None:
        """关闭服务资源。"""
        try:
            await self._mcp_stack.aclose()
        except Exception:
            logger.exception("Error closing MCP toolsets")

    # ------------------------------------------------------------------ MCP 管理

    def _get_mcp_config_signature(self) -> tuple[float, int] | None:
        return get_mcp_config_signature(self.config.user_dir, self._mcp_config_signature)

    async def _maybe_reload_mcp_toolsets(self) -> None:
        signature = self._get_mcp_config_signature()
        if signature == self._mcp_config_signature:
            return
        await self._reload_mcp_toolsets(force=True)

    async def _reload_mcp_toolsets(self, force: bool = False) -> None:
        async with self._mcp_reload_lock:
            signature = self._get_mcp_config_signature()
            if not force and signature == self._mcp_config_signature:
                return

            logger.info("reloading MCP toolsets signature_changed=%s", signature != self._mcp_config_signature)
            result = await load_new_mcp_stack(self.config, signature)

            old_stack = self._mcp_stack
            self.config = result.new_config
            self._mcp_stack = result.new_stack
            self._mcp_toolsets = result.new_toolsets
            self._mcp_config_signature = result.new_signature
            self._mcp_errors = result.errors
            self._agents.clear()
            self._tool_catalog_cache.clear()
            await old_stack.aclose()
            logger.info(
                "MCP toolsets reloaded connected=%d errors=%d",
                len(result.new_toolsets),
                len(result.errors),
            )

    # ------------------------------------------------------------------ 角色与会话身份

    def _normalize_role(self, role_name: str | None) -> str:
        return validate_role_name(role_name or self.config.default_role_name)

    def _normalize_session_id(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        return validate_session_id(session_id, allow_legacy=False)

    def _validate_role_exists(self, role_name: str) -> None:
        if role_name == self.config.default_role_name and not list_role_names(self.config.roles_dir):
            return
        load_role_content(self.config.roles_dir, role_name)

    def state(self) -> AgentState:
        return AgentState(
            model=self.config.model_name,
            base_url=self.config.openai_base_url or None,
            mcp_servers=len(self._mcp_toolsets),
            mcp_errors=list(self._mcp_errors),
        )

    def list_roles(self) -> RolesResponse:
        logger.debug("listing roles roles_dir=%s", self.config.roles_dir)
        return RolesResponse(
            roles=list_role_names(self.config.roles_dir),
            current_role=self.config.default_role_name,
        )

    def latest_session(self, role_name: str | None = None, latest_scope: str | None = None) -> CommandResponse:
        role = self._normalize_role(role_name)
        self._validate_role_exists(role)
        scope = validate_latest_scope(latest_scope)
        session_id = self.session_store.get_latest_session_id(role, scope)
        if not session_id:
            session_id = self.session_store.new_session_id()
            self.session_store.set_latest_session_id(role, session_id, scope)
            logger.info("no latest session found, created new session role=%s session_id=%s scope=%s", role, session_id, scope)
        else:
            logger.info("loaded latest session role=%s session_id=%s scope=%s", role, session_id, scope)

        return CommandResponse(
            message="已恢复上一次的对话上下文。",
            role=role,
            session_id=session_id,
        )

    def new_session(self, role_name: str | None = None, latest_scope: str | None = None) -> CommandResponse:
        role = self._normalize_role(role_name)
        self._validate_role_exists(role)
        scope = validate_latest_scope(latest_scope)
        session_id = self.session_store.start_new_session(role, scope)
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
            f"🤖 模型:{self.config.model_name}",
        ]
        if self.config.openai_base_url:
            lines.append(f"🌐 模型服务:{self.config.openai_base_url}")
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

    def switch_role(self, role_name: str, latest_scope: str | None = None) -> CommandResponse:
        role = self._normalize_role(role_name)
        self._validate_role_exists(role)
        scope = validate_latest_scope(latest_scope)
        session_id = self.session_store.start_new_session(role, scope)
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
        self,
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

        role = self._normalize_role(role_name)
        self._validate_role_exists(role)
        if channel_code not in CHANNEL_CODES:
            channel_code = DEFAULT_CHANNEL
        scope = validate_latest_scope(latest_scope or channel_code)
        # start_new_session 内部:写 sessions(channel 首写定终身)+ 写 pointer,一并完成。
        session_id = self.session_store.start_new_session(
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

    # ------------------------------------------------------------------ 会话管理（WebUI）

    # WebUI 展示历史会话时需要的 metadata 键名。
    #
    # ``openhachimi_user_message``  —— 用户原始输入（turn.py 持久化时写入）。
    # ``openhachimi_ctx_dynamic``   —— 本轮 system prompt 末尾的动态段
    #     (时间/TaskFrame/记忆/技能),由 turn.py 持久化时写入。
    # ``openhachimi_ctx_static_hash`` —— 稳定段(base/executor/role/config/tools)
    #     的短哈希;完整文本经 _resolve_static_context 在内存池中查表回填。
    # ``openhachimi_system_context``  —— 旧版本(v2)的整段快照,读取时作为兜底。
    _USER_MESSAGE_METADATA_KEY = "openhachimi_user_message"
    _CTX_DYNAMIC_METADATA_KEY = "openhachimi_ctx_dynamic"
    _CTX_STATIC_HASH_METADATA_KEY = "openhachimi_ctx_static_hash"
    _SYSTEM_CONTEXT_METADATA_KEY = "openhachimi_system_context"  # legacy

    def _extract_text_parts(
        self, messages: list[ModelMessage], role: str | None = None,
    ) -> list[dict]:
        """将 ``pydantic_ai.messages.ModelMessage`` 列表转为简单的 ``{role, content, prefix, timestamp, tokens}`` 结构。

        遍历 ``ModelRequest``（用户消息）中的 ``UserPromptPart``，
        以及 ``ModelResponse``（Agent 回复）中的 ``TextPart``，
        忽略工具调用、工具返回等中间环节。

        user 消息额外返回 ``prefix`` 字段（运行时注入的可折叠上下文）。读取优先级：
          1. v3:同时读 ``openhachimi_ctx_dynamic`` + ``openhachimi_ctx_static_hash``,
             由 ``_resolve_static_context(role, hash)`` 查池/重建静态段;
             prefix = dynamic + "\\n\\n" + static。
          2. v2 兜底:``openhachimi_system_context`` 整段(老会话历史)。
          3. v1 兜底:从 UserPromptPart 全文中拆出 ``openhachimi_user_message`` 之前的前缀。
          4. 兜底:prefix = ""。

        每条消息都会带上 ISO-8601 ``timestamp``：user 取 ``ModelRequest.timestamp``，
        assistant 取 ``ModelResponse.timestamp``，找不到时为 None。
        assistant 消息额外返回 ``tokens={"input", "output", "total", "cache_read"}``
        (来自 ``ModelResponse.usage``);旧会话 / 缺失 usage 时为 None。
        """
        from pydantic_ai.messages import TextPart, UserPromptPart

        result: list[dict] = []
        for msg in messages:
            msg_ts = getattr(msg, "timestamp", None)
            ts_iso = msg_ts.isoformat() if msg_ts is not None else None
            if isinstance(msg, ModelRequest):
                metadata = getattr(msg, "metadata", None) or {}
                if not isinstance(metadata, dict):
                    metadata = {}
                user_message_meta = metadata.get(self._USER_MESSAGE_METADATA_KEY)
                dynamic_meta = metadata.get(self._CTX_DYNAMIC_METADATA_KEY)
                static_hash_meta = metadata.get(self._CTX_STATIC_HASH_METADATA_KEY)
                legacy_system_context_meta = metadata.get(self._SYSTEM_CONTEXT_METADATA_KEY)

                for part in getattr(msg, "parts", ()):
                    if isinstance(part, UserPromptPart):
                        raw = part.content
                        if isinstance(raw, str):
                            text = raw
                        else:
                            # content 可能是 Sequence[UserContent]，取所有文本片段
                            text = " ".join(str(x) for x in raw if isinstance(x, str))
                        if not text.strip():
                            continue

                        # 优先采用 UserPromptPart 自带 timestamp（更接近"用户实际发送时刻"）
                        part_ts = getattr(part, "timestamp", None)
                        item_ts = part_ts.isoformat() if part_ts is not None else ts_iso

                        # ---- v3 路径：分段 metadata + 静态池回填 ----
                        prefix_v3 = ""
                        if isinstance(dynamic_meta, str) and dynamic_meta.strip():
                            prefix_v3 = dynamic_meta.strip()
                        if isinstance(static_hash_meta, str) and static_hash_meta:
                            static_text = self._resolve_static_context(role, static_hash_meta)
                            if static_text:
                                prefix_v3 = f"{prefix_v3}\n\n{static_text}" if prefix_v3 else static_text
                        if prefix_v3:
                            if isinstance(user_message_meta, str) and user_message_meta:
                                content = user_message_meta
                            else:
                                content = text.strip()
                            result.append({
                                "role": "user", "content": content, "prefix": prefix_v3,
                                "timestamp": item_ts, "tokens": None,
                            })
                            break

                        # ---- v2 路径：旧整段快照(老会话) ----
                        if isinstance(legacy_system_context_meta, str) and legacy_system_context_meta.strip():
                            prefix = legacy_system_context_meta.strip()
                            if isinstance(user_message_meta, str) and user_message_meta:
                                content = user_message_meta
                            else:
                                content = text.strip()
                            result.append({
                                "role": "user", "content": content, "prefix": prefix,
                                "timestamp": item_ts, "tokens": None,
                            })
                            break

                        # ---- 旧路径：有 user_message 但无 system_context 快照 ----
                        if isinstance(user_message_meta, str) and user_message_meta:
                            user_msg = user_message_meta
                            stripped = text.rstrip()
                            if stripped.endswith(user_msg):
                                prefix = stripped[: -len(user_msg)].rstrip("\n").rstrip()
                            else:
                                logger.debug(
                                    "user_msg not at end of UserPromptPart; using metadata only "
                                    "user_msg_chars=%d prompt_chars=%d prompt_preview=%r",
                                    len(user_msg),
                                    len(stripped),
                                    stripped[:120],
                                )
                                prefix = ""
                            result.append({
                                "role": "user", "content": user_msg, "prefix": prefix,
                                "timestamp": item_ts, "tokens": None,
                            })
                            break

                        # ---- 兜底：旧会话无 metadata，整段显示 ----
                        result.append({
                            "role": "user", "content": text.strip(), "prefix": "",
                            "timestamp": item_ts, "tokens": None,
                        })
                        break
            elif isinstance(msg, ModelResponse):
                # 把 ModelResponse.usage 抽成 {input, output, total, cache_read}。
                # pydantic_ai 的 RequestUsage 字段为 input_tokens / output_tokens /
                # cache_read_tokens / cache_write_tokens 等。展示层关心:
                # - input/output:本轮净读写
                # - total:输入+输出(不含 cache 复算)
                # - cache_read:缓存命中(KV cache hit),反映省钱/提速能力
                # cache_write 不展示(噪声大,模型缓存调度对用户透明)。
                usage = getattr(msg, "usage", None)
                tokens_dict: dict[str, int] | None = None
                if usage is not None:
                    try:
                        input_t = int(getattr(usage, "input_tokens", 0) or 0)
                        output_t = int(getattr(usage, "output_tokens", 0) or 0)
                        cache_read_t = int(getattr(usage, "cache_read_tokens", 0) or 0)
                        if input_t or output_t:
                            tokens_dict = {
                                "input": input_t,
                                "output": output_t,
                                "total": input_t + output_t,
                                "cache_read": cache_read_t,
                            }
                    except (TypeError, ValueError):
                        tokens_dict = None
                for part in getattr(msg, "parts", ()):
                    if isinstance(part, TextPart):
                        text = str(part.content).strip()
                        if text:
                            result.append({
                                "role": "assistant", "content": text, "prefix": "",
                                "timestamp": ts_iso, "tokens": tokens_dict,
                            })
        return result

    def list_sessions(
        self,
        role_name: str | None = None,
        *,
        with_preview: bool = True,
        channel: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        """列出指定角色的所有历史会话(支持分页)。

        ``channel`` 非空时按渠道过滤(SessionStore 内部对未知渠道做 DEFAULT_CHANNEL 兜底)。
        ``limit/offset`` 是 offset-based 分页,``limit=None`` 时不分页(老调用方兜底)。
        返回 ``{"role": str, "sessions": [...], "total": int, "limit": int|None, "offset": int}`` —— 前端用
        ``total`` 判定 ``hasMore``;切渠道 / 切角色时前端重置 offset 重新拉第一页。
        """
        role = self._normalize_role(role_name)
        self._validate_role_exists(role)
        # store 端已经做了渠道过滤;传非法 channel 会被忽略(返回全部),与旧语义一致。
        raw = self.session_store.list_sessions(role, channel=channel, limit=limit, offset=offset)
        total = self.session_store.count_sessions(role, channel=channel)

        sessions: list[dict] = []
        for s in raw:
            sid = s["session_id"]
            created_at: str | None = None
            # 解析 session_id 前缀 "YYYYMMDD-HHMMSS-..." 还原 created_at(展示用)。
            if "-" in sid:
                try:
                    dt = datetime.strptime(sid[:15], "%Y%m%d-%H%M%S")
                    created_at = dt.isoformat()
                except (ValueError, IndexError):
                    pass

            preview = ""
            msg_count = 0
            if with_preview:
                try:
                    _, msgs = self.session_store.load_messages(role, sid)
                except Exception:
                    msgs = []
                msg_count = len(msgs)
                parts = self._extract_text_parts(msgs, role=role)
                user_msgs = [p["content"] for p in parts if p["role"] == "user"]
                if user_msgs:
                    preview = user_msgs[0][:80]

            sessions.append({
                "session_id": sid,
                "role": role,
                "created_at": created_at,
                "mtime": s["mtime"],
                "preview": preview,
                "message_count": msg_count,
                "channel": s.get("channel", "webui"),
            })

        return {
            "role": role,
            "sessions": sessions,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def load_session(self, role_name: str | None = None, session_id: str | None = None, latest_scope: str | None = None) -> CommandResponse:
        """切换到指定会话——只做存在性校验,不再写全局 ``latest``。

        旧实现会把目标 session_id 写到 ``latest_scope`` 对应的 latest 指针,这是
        跨渠道串号的根因:WebUI 在侧栏点开某条 IM 会话查看时,会污染全局 latest,
        导致下一次 WebUI 不带 ``session_id`` 发消息时把消息追加到 IM 的 .json。
        现在 load_session 只检查目标会话存在;前端把它写入 currentSessionId,
        后续发送时显式带上 session_id 即可。
        """
        role = self._normalize_role(role_name)
        self._validate_role_exists(role)
        resolved_session_id = self._normalize_session_id(session_id)
        if not resolved_session_id:
            raise ValueError("session_id 不能为空，请指定要加载的会话")

        # 触发存在性校验:不存在时让上层把错误透传给前端
        if not self.session_store.session_exists(role, resolved_session_id):
            raise FileNotFoundError(f"会话不存在: {resolved_session_id}")
        _ = validate_latest_scope(latest_scope)
        logger.info("loaded session role=%s session_id=%s", role, resolved_session_id)
        return CommandResponse(
            message="已切换到指定会话。",
            role=role,
            session_id=resolved_session_id,
        )

    def get_session_messages(self, role_name: str | None = None, session_id: str | None = None) -> dict:
        role = self._normalize_role(role_name)
        self._validate_role_exists(role)
        resolved_session_id = self._normalize_session_id(session_id)
        if not resolved_session_id:
            raise ValueError("session_id 不能为空")

        _, msgs = self.session_store.load_messages(role, resolved_session_id)
        parts = self._extract_text_parts(msgs, role=role)

        from openhachimi_agent.transport.api_models import MessageItem

        return {
            "role": role,
            "session_id": resolved_session_id,
            "messages": [MessageItem(**p) for p in parts],
        }

    # ------------------------------------------------------------------ 中断与停止

    async def interrupt_session_resources(self, session_id: str, reason: str = "interrupt") -> int:
        session_id = validate_session_id(session_id, allow_legacy=False)
        state = self._session_states.setdefault(session_id, {})
        state["cancel_requested"] = True
        state["cancel_reason"] = reason
        state["last_cancelled_at"] = time.time()
        return await self._interrupt_session_resources(session_id)

    async def stop_session(self, session_id: str) -> CommandResponse:
        session_id = validate_session_id(session_id, allow_legacy=False)
        logger.info("stop requested for session_id=%s", session_id)
        task = self._running_tasks.get(session_id)
        interrupted_count = await self.interrupt_session_resources(session_id, reason="user_stop")

        if task is not None:
            if not task.done():
                task.cancel()
                task.add_done_callback(self._log_cancelled_task_result)
            return CommandResponse(
                message="已成功中断当前任务。",
                role=self.config.default_role_name,
                session_id=session_id,
            )
        if interrupted_count:
            return CommandResponse(
                message="已成功中断当前任务。",
                role=self.config.default_role_name,
                session_id=session_id,
            )
        return CommandResponse(
            message="当前没有正在运行的任务。",
            role=self.config.default_role_name,
            session_id=session_id,
        )

    def _log_cancelled_task_result(self, task: asyncio.Task) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.debug("cancelled task finished with error: %s", exc)

    async def _interrupt_session_resources(self, session_id: str) -> int:
        terminate_session = getattr(self.process_manager, "terminate_session", None)
        if not callable(terminate_session):
            return 0
        try:
            count = await asyncio.to_thread(terminate_session, session_id)
        except Exception:
            logger.exception("failed to interrupt resources for session_id=%s", session_id)
            return 0
        logger.info("interrupted session resources session_id=%s process_count=%s", session_id, count)
        return count if isinstance(count, int) else 0

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    # ------------------------------------------------------------------ 优先命令分发

    def _resolve_priority_session(
        self,
        role: str | None,
        session_id: str | None,
        latest_scope: str | None = None,
    ) -> tuple[str, str]:
        resolved_role = self._normalize_role(role)
        resolved_session_id = self._normalize_session_id(session_id)
        if resolved_session_id:
            return resolved_role, resolved_session_id
        latest = self.latest_session(resolved_role, latest_scope=latest_scope)
        return latest.role, latest.session_id

    async def dispatch_command(
        self,
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
        return await spec.handler(self, args, role, session_id, channel_context, channel)

    async def _handle_priority_command_response(
        self,
        message: str,
        role: str | None,
        session_id: str | None,
        channel_context: dict[str, object] | None = None,
        channel: str = "local",
    ) -> ChatResponse | None:
        outcome = await self.dispatch_command(
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
        resolved_role, resolved_session_id = self._resolve_priority_session(role, session_id, latest_scope)
        return ChatResponse(
            output=outcome.message,
            role=outcome.role or resolved_role,
            session_id=outcome.session_id or resolved_session_id,
        )

    async def _handle_priority_command_events(
        self,
        message: str,
        role: str | None,
        session_id: str | None,
        channel_context: dict[str, object] | None = None,
        channel: str = "local",
    ) -> list[StreamEventItem] | None:
        response = await self._handle_priority_command_response(
            message, role, session_id, channel_context, channel=channel,
        )
        if response is None:
            return None
        # 斜杠命令的输出本身是要给用户看的正文,走 type="text"。
        # type="system" 现在专表"运行时状态提示",会在 stream_events 出口处被统一过滤掉。
        return [StreamEventItem(type="text", text=response.output)]

    # ------------------------------------------------------------------ 上下文压缩

    async def compress_session(
        self,
        role: str,
        session_id: str,
        focus_topic: str = "",
        latest_scope: str | None = None,
    ) -> ChatResponse:
        """手动压缩指定会话的上下文历史(可带焦点主题)。

        与 turn.py 的 ``run_turn`` 一样套住 per-session asyncio.Lock —— 旧实现
        没套,理论上一次手动 /compress 可能在 turn 写入 session 的瞬间插队读旧
        history 然后用陈旧值覆盖回去。SQLite 端虽有 ``BEGIN IMMEDIATE`` 兜底,
        应用层这把锁还是要拿,belt-and-suspenders。
        """
        role = self._normalize_role(role)
        actual_session_id, history = self.session_store.load_messages(role, session_id, latest_scope)
        if not history:
            return ChatResponse(output="当前会话无历史可压缩。", role=role, session_id=actual_session_id)
        memory_scope = MemoryScope(
            tenant_id="local",
            user_id="local",
            role_name=role,
            session_id=actual_session_id,
            channel="local",
        )
        compressor = self._get_context_compressor(actual_session_id, memory_scope)
        if compressor is None:
            return ChatResponse(output="上下文压缩未启用。", role=role, session_id=actual_session_id)
        if not compressor.has_content_to_compress(history):
            return ChatResponse(output="当前对话历史较短,暂无需压缩。", role=role, session_id=actual_session_id)
        focus = focus_topic.strip() or None
        before = len(history)
        try:
            compressed = await asyncio.to_thread(
                compressor.compress,
                history,
                focus_topic=focus,
                force=True,
            )
        except Exception as exc:
            logger.warning("manual compress failed role=%s session_id=%s: %s", role, actual_session_id, exc)
            return ChatResponse(output=f"压缩失败:{exc.__class__.__name__}", role=role, session_id=actual_session_id)
        if len(compressed) >= before:
            return ChatResponse(
                output=f"未产生压缩(可能已无可压缩的中间窗口)。历史共 {before} 条消息。",
                role=role,
                session_id=actual_session_id,
            )
        # 写回阶段:套 session lock,与 turn.py 的写入路径互斥。
        lock = self._get_session_lock(actual_session_id)
        async with lock:
            await asyncio.to_thread(
                self.session_store.save_messages,
                role,
                actual_session_id,
                compressed,
                scope=latest_scope,
            )
        savings = compressor._last_compression_savings_pct  # noqa: SLF001
        focus_hint = f"(焦点:{focus})" if focus else ""
        return ChatResponse(
            output=f"已压缩上下文{focus_hint}:{before}→{len(compressed)} 条消息(第 {compressor.compression_count} 次压缩,约省 {savings:.0f}%)。",
            role=role,
            session_id=actual_session_id,
        )

    def _get_context_compressor(self, session_id: str, memory_scope: MemoryScope) -> Any:
        """获取或构建会话级上下文压缩器(含 LLM 摘要器与记忆抢救钩子)。"""
        cached = self._context_compressors.get(session_id)
        if cached is not None:
            return cached
        cfg = self.config.context
        if not cfg.enabled:
            return None
        from openhachimi_agent.context.compressor import ContextCompressor
        from openhachimi_agent.context.summary import build_summarizer
        from openhachimi_agent.memory.capture import capture_compressed_window

        summarizer = build_summarizer(self.config)

        def _rescue(full_messages: list, window: list) -> None:
            capture_compressed_window(self.config, memory_scope, full_messages, window)

        compressor = ContextCompressor(
            threshold_percent=cfg.threshold_percent,
            hard_ceiling_percent=cfg.hard_ceiling_percent,
            protect_first_n=cfg.protect_first_n,
            protect_last_n=cfg.protect_last_n,
            tail_token_budget=cfg.tail_token_budget,
            anti_thrash=cfg.anti_thrash,
            min_savings_pct=cfg.min_savings_pct,
            # context_length 配置单位为 K,这里换算成 token(128K = 128000)传给压缩引擎
            context_length=cfg.context_length * 1000 if cfg.context_length else 0,
            abort_on_summary_failure=cfg.summary.abort_on_failure,
            summarizer=summarizer,
            pre_compress_callback=_rescue,
        )
        # 让 token 计数用与实际模型匹配的 tiktoken encoding(gpt-4o 系 -> o200k_base,
        # gpt-4 系 -> cl100k_base;未知第三方模型名回退默认),提升压缩预检/边界精度。
        from openhachimi_agent.context.token_estimate import set_model_for_token_estimate
        set_model_for_token_estimate(self.config.model_name)
        self._context_compressors[session_id] = compressor
        return compressor

    # ------------------------------------------------------------------ 工具目录缓存

    def _get_cached_tool_catalog(self, role: str, executor_agent: object) -> str:
        """按 role + 当前签名返回缓存的工具目录摘要,未命中或过期则重建。

        ``turn.py`` 在 ``_build_executor_static_context`` 中调用此方法获取
        工具清单,不再每轮遍历所有 toolset。
        """
        key = f"{role}:executor"
        sig = (self._mcp_config_signature, self._agent_dependency_mtime_cache)
        cached = self._tool_catalog_cache.get(key)
        if cached is not None and cached[1] == sig:
            return cached[0]
        try:
            from openhachimi_agent.service.agent_runtime.turn import _extract_tool_catalog

            text = _extract_tool_catalog(executor_agent)
        except Exception:
            logger.debug("tool catalog extraction failed for role=%s", role, exc_info=True)
            text = ""
        self._tool_catalog_cache[key] = (text, sig)
        return text

    # ------------------------------------------------------------------ 上下文静态池

    def _ensure_context_static(self, hash_key: str, text: str) -> None:
        """将静态 system prompt 段写入进程内池(de facto 去重)。"""
        if hash_key and text and not self._context_static_pool.get(hash_key):
            self._context_static_pool[hash_key] = text

    def _resolve_static_context(self, role: str | None, hash_key: str) -> str:
        """从进程内池按 hash 取出静态段;池中不存在时尝试按 role 重建当前版本。

        重建仅在 hash 与当前版本一致时写入池(避免攒入旧 hash),不一致时静默降级。
        """
        if not hash_key:
            return ""
        text = self._context_static_pool.get(hash_key)
        if text is not None:
            return text
        # 池中不存在:尝试重建当前版本的静态段,若哈希相同则写入池
        if role:
            try:
                executor_agent = self._get_agent(role, "executor")
            except Exception:
                return ""
            try:
                from openhachimi_agent.service.agent_runtime.turn import _build_executor_static_context, _compute_static_hash

                rebuilt = _build_executor_static_context(self.config, role, executor_agent, service=self)
                rebuilt_hash = _compute_static_hash(rebuilt)
                if rebuilt_hash == hash_key:
                    self._context_static_pool[hash_key] = rebuilt
                    return rebuilt
                # 哈希不匹配 → 配置或依赖已变,旧 hash 的静态段已无意义,不写入
            except Exception:
                logger.debug("failed to rebuild static context for hash=%s role=%s", hash_key, role, exc_info=True)
        return ""

    # ------------------------------------------------------------------ Agent 构建与工件

    def _get_agent_dependency_mtime(self, role_name: str) -> float:
        mtime, new_cache = compute_dependency_mtime(self.config, role_name, self._agent_dependency_mtime_cache)
        self._agent_dependency_mtime_cache = new_cache
        return mtime

    def _get_agent(self, role_name: str, agent_type: str = "executor"):
        current_mtime = self._get_agent_dependency_mtime(role_name)
        return get_or_build_agent(
            self._agents,
            self.config,
            role_name,
            agent_type,
            self._mcp_toolsets,
            current_mtime,
        )

    def register_artifacts(self, artifacts: list[ArtifactRef]) -> None:
        for artifact in artifacts:
            self._artifact_records[artifact.id] = artifact

    def get_artifact(self, artifact_id: str) -> ArtifactRef | None:
        return self._artifact_records.get(artifact_id)

    # ------------------------------------------------------------------ 对外消息入口

    def _run_with_session(
        self,
        message: str,
        role: str | None,
        session_id: str | None,
        *,
        stream: bool,
        attachments: Sequence[AttachmentRef] | None = None,
        run_mode: str = "interactive",
        channel_context: dict[str, object] | None = None,
        scheduler_context: dict[str, object] | None = None,
        channel: str | None = None,
        delivery_target: dict[str, object] | None = None,
    ) -> AsyncIterator[object]:
        """委托到 `agent_runtime.turn.run_turn`。

        保留此薄壳是为了让测试可以通过 `service._run_with_session = ...` 打桩。
        """
        return run_turn(
            self,
            message,
            role,
            session_id,
            stream=stream,
            attachments=attachments,
            run_mode=run_mode,
            channel_context=channel_context,
            scheduler_context=scheduler_context,
            channel=channel,
            delivery_target=delivery_target,
        )

    async def send_message(
        self,
        message: str,
        role: str | None = None,
        session_id: str | None = None,
        attachments: Sequence[AttachmentRef] | None = None,
        run_mode: str = "interactive",
        channel_context: dict[str, object] | None = None,
        scheduler_context: dict[str, object] | None = None,
        channel: str = "local",
        delivery_target: dict[str, object] | None = None,
    ) -> ChatResponse:
        priority_response = await self._handle_priority_command_response(
            message, role, session_id, channel_context, channel=channel,
        )
        if priority_response is not None:
            return priority_response

        async for result in self._run_with_session(
            message,
            role,
            session_id,
            stream=False,
            attachments=attachments,
            run_mode=run_mode,
            channel_context=channel_context,
            scheduler_context=scheduler_context,
            channel=channel,
            delivery_target=delivery_target,
        ):
            return result  # type: ignore[return-value]
        raise RuntimeError("No result returned from run_turn")

    async def stream_events(
        self,
        message: str,
        role: str | None = None,
        session_id: str | None = None,
        attachments: Sequence[AttachmentRef] | None = None,
        run_mode: str = "interactive",
        channel_context: dict[str, object] | None = None,
        scheduler_context: dict[str, object] | None = None,
        channel: str = "local",
        delivery_target: dict[str, object] | None = None,
    ) -> AsyncIterator[StreamEventItem]:
        priority_events = await self._handle_priority_command_events(
            message, role, session_id, channel_context, channel=channel,
        )
        if priority_events is not None:
            for event in priority_events:
                yield event
            return

        async for event in self._run_with_session(
            message,
            role,
            session_id,
            stream=True,
            attachments=attachments,
            run_mode=run_mode,
            channel_context=channel_context,
            scheduler_context=scheduler_context,
            channel=channel,
            delivery_target=delivery_target,
        ):
            if isinstance(event, StreamEventItem):
                if event.type == "tool" and not self.config.show_tool_calls:
                    continue
                # 运行时状态提示(planner heartbeat / replan / 视觉模型 / 最终验证补齐等)
                # 对最终用户没有帮助,只会让 Telegram / 前端的对话流变得嘈杂。
                # 统一在 stream 出口屏蔽掉,内部仍走日志可查。
                if event.type == "system":
                    continue
                yield event

    async def stream_message(
        self,
        message: str,
        role: str | None = None,
        session_id: str | None = None,
        attachments: Sequence[AttachmentRef] | None = None,
    ) -> AsyncIterator[str]:
        async for event in self.stream_events(message, role, session_id, attachments=attachments):
            if event.type == "text":
                yield event.text
            elif event.type == "tool":
                yield f"\n[工具] {event.text}\n"
