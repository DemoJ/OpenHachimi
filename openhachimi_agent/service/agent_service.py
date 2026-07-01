"""Agent 后台服务层。

本模块是 ``AgentService`` 的外观层(facade):持有运行态字段与各子系统实例,
对外暴露统一的会话/消息/命令 API。具体逻辑下沉到 ``service/agent_runtime/``
下的纯函数模块,本文件内方法退化为「取自身字段 → 调函数」的薄壳,签名与字段名
保持稳定,供 ``turn.py`` / HTTP / CLI / Telegram / 微信 / 测试直接访问。
"""

import asyncio
import contextlib
import logging
import weakref
from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic_ai.messages import ModelMessage

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.service.agent_runtime import (
    command_dispatch,
    context_cache,
    context_compression,
    session_commands,
    session_control,
    session_history,
)
from openhachimi_agent.service.agent_runtime.agent_cache import (
    compute_dependency_mtime,
    get_or_build_agent,
)
from openhachimi_agent.service.agent_runtime.command_registry import CommandOutcome
from openhachimi_agent.service.agent_runtime.mcp_manager import (
    get_mcp_config_signature,
    load_new_mcp_stack,
)
from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem
from openhachimi_agent.service.agent_runtime.turn import run_turn
from openhachimi_agent.transport.api_models import (
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
        return session_commands.normalize_role(self, role_name)

    def _normalize_session_id(self, session_id: str | None) -> str | None:
        return session_commands.normalize_session_id(session_id)

    def _validate_role_exists(self, role_name: str) -> None:
        return session_commands.validate_role_exists(self, role_name)

    def state(self):
        return session_commands.state(self)

    def list_roles(self) -> RolesResponse:
        return session_commands.list_roles(self)

    def latest_session(self, role_name: str | None = None, latest_scope: str | None = None) -> CommandResponse:
        return session_commands.latest_session(self, role_name, latest_scope)

    def new_session(self, role_name: str | None = None, latest_scope: str | None = None) -> CommandResponse:
        return session_commands.new_session(self, role_name, latest_scope)

    def switch_role(self, role_name: str, latest_scope: str | None = None) -> CommandResponse:
        return session_commands.switch_role(self, role_name, latest_scope)

    def new_session_for_channel(
        self,
        role_name: str | None,
        channel_code: str,
        *,
        latest_scope: str | None = None,
    ) -> CommandResponse:
        """为指定渠道新建会话并立即在 SessionStore 写入渠道归属。详见 ``session_commands.new_session_for_channel``。"""
        return session_commands.new_session_for_channel(
            self, role_name, channel_code, latest_scope=latest_scope,
        )

    # ------------------------------------------------------------------ 会话管理（WebUI）

    # WebUI 展示历史会话时使用的 metadata 键名(详见 session_history 模块)。
    # 保留这些类属性以兼容历史外部引用(测试 / 调试可能按名读取)。
    _USER_MESSAGE_METADATA_KEY = session_history.USER_MESSAGE_METADATA_KEY
    _CTX_DYNAMIC_METADATA_KEY = session_history.CTX_DYNAMIC_METADATA_KEY
    _CTX_STATIC_HASH_METADATA_KEY = session_history.CTX_STATIC_HASH_METADATA_KEY
    _SYSTEM_CONTEXT_METADATA_KEY = session_history.SYSTEM_CONTEXT_METADATA_KEY  # legacy

    def _extract_text_parts(
        self, messages: list[ModelMessage], role: str | None = None,
    ) -> list[dict]:
        """将 ``ModelMessage`` 列表转为展示用文本结构。详见 ``session_history.extract_text_parts``。"""
        return session_history.extract_text_parts(self, messages, role=role)

    def list_sessions(
        self,
        role_name: str | None = None,
        *,
        with_preview: bool = True,
        channel: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        """列出指定角色的所有历史会话(支持分页)。详见 ``session_history.list_sessions``。"""
        return session_history.list_sessions(
            self, role_name,
            with_preview=with_preview, channel=channel, limit=limit, offset=offset,
        )

    def load_session(self, role_name: str | None = None, session_id: str | None = None, latest_scope: str | None = None) -> CommandResponse:
        """切换到指定会话——只做存在性校验。详见 ``session_history.load_session``。"""
        return session_history.load_session(self, role_name, session_id, latest_scope)

    def delete_session(self, role_name: str | None = None, session_id: str | None = None) -> CommandResponse:
        """删除指定会话。详见 ``session_history.delete_session``。"""
        return session_history.delete_session(self, role_name, session_id)

    def get_session_messages(self, role_name: str | None = None, session_id: str | None = None, limit: int | None = None, before_turn: int | None = None) -> dict:
        return session_history.get_session_messages(self, role_name, session_id, limit=limit, before_turn=before_turn)

    def get_folded_messages(
        self, role_name: str | None, session_id: str, compression_id: int
    ) -> list[dict]:
        """返回某次压缩被折叠的原始消息(展开用)。详见 ``session_history.get_folded_messages``。"""
        return session_history.get_folded_messages(self, role_name, session_id, compression_id)

    # ------------------------------------------------------------------ 中断与停止

    async def interrupt_session_resources(self, session_id: str, reason: str = "interrupt") -> int:
        return await session_control.interrupt_session_resources(self, session_id, reason)

    async def stop_session(self, session_id: str) -> CommandResponse:
        return await session_control.stop_session(self, session_id)

    def _log_cancelled_task_result(self, task: asyncio.Task) -> None:
        return session_control._log_cancelled_task_result(task)

    async def _interrupt_session_resources(self, session_id: str) -> int:
        return await session_control._interrupt_session_resources(self, session_id)

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        return session_control.get_session_lock(self, session_id)

    # ------------------------------------------------------------------ 优先命令分发

    def _resolve_priority_session(
        self,
        role: str | None,
        session_id: str | None,
        latest_scope: str | None = None,
    ) -> tuple[str, str]:
        return command_dispatch.resolve_priority_session(self, role, session_id, latest_scope)

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
        return await command_dispatch.dispatch_command(
            self, message,
            role=role, session_id=session_id,
            channel_context=channel_context, channel=channel,
        )

    async def _handle_priority_command_response(
        self,
        message: str,
        role: str | None,
        session_id: str | None,
        channel_context: dict[str, object] | None = None,
        channel: str = "local",
    ) -> ChatResponse | None:
        return await command_dispatch.handle_priority_command_response(
            self, message, role, session_id, channel_context, channel=channel,
        )

    async def _handle_priority_command_events(
        self,
        message: str,
        role: str | None,
        session_id: str | None,
        channel_context: dict[str, object] | None = None,
        channel: str = "local",
    ) -> list[StreamEventItem] | None:
        return await command_dispatch.handle_priority_command_events(
            self, message, role, session_id, channel_context, channel=channel,
        )

    # ------------------------------------------------------------------ 上下文压缩

    async def compress_session(
        self,
        role: str,
        session_id: str,
        focus_topic: str = "",
        latest_scope: str | None = None,
    ) -> ChatResponse:
        """手动压缩指定会话的上下文历史(可带焦点主题)。详见 ``context_compression.compress_session``。"""
        return await context_compression.compress_session(
            self, role, session_id, focus_topic=focus_topic, latest_scope=latest_scope,
        )

    def _get_context_compressor(self, session_id: str) -> Any:
        """获取或构建会话级上下文压缩器(含 LLM 摘要器)。详见 ``context_compression.get_context_compressor``。"""
        return context_compression.get_context_compressor(self, session_id)

    # ------------------------------------------------------------------ 工具目录缓存

    def _get_cached_tool_catalog(self, role: str, executor_agent: object) -> str:
        """按 role + 当前签名返回缓存的工具目录摘要。详见 ``context_cache.get_cached_tool_catalog``。"""
        return context_cache.get_cached_tool_catalog(self, role, executor_agent)

    # ------------------------------------------------------------------ 上下文静态池

    def _ensure_context_static(self, hash_key: str, text: str) -> None:
        """将静态 system prompt 段写入进程内池(de facto 去重)。详见 ``context_cache.ensure_context_static``。"""
        return context_cache.ensure_context_static(self, hash_key, text)

    def _resolve_static_context(self, role: str | None, hash_key: str) -> str:
        """从进程内池按 hash 取出静态段;池中不存在时尝试按 role 重建当前版本。详见 ``context_cache.resolve_static_context``。"""
        return context_cache.resolve_static_context(self, role, hash_key)

    # ------------------------------------------------------------------ Agent 构建与工件

    def _get_agent_dependency_mtime(self, role_name: str) -> float:
        mtime, new_cache = compute_dependency_mtime(self.config, role_name, self._agent_dependency_mtime_cache)
        self._agent_dependency_mtime_cache = new_cache
        return mtime

    def _get_agent(self, role_name: str, agent_type: str = "main", run_mode: str = "interactive"):
        current_mtime = self._get_agent_dependency_mtime(role_name)
        return get_or_build_agent(
            self._agents,
            self.config,
            role_name,
            agent_type,
            self._mcp_toolsets,
            current_mtime,
            run_mode=run_mode,
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
