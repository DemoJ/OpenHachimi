"""Agent 后台服务层。"""

import asyncio
import contextlib
import logging
import time
import weakref
from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic_ai import ModelMessagesTypeAdapter

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
    latest_scope_from_context,
)
from openhachimi_agent.service.agent_runtime.mcp_manager import (
    get_mcp_config_signature,
    load_new_mcp_stack,
)
from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem
from openhachimi_agent.service.agent_runtime.turn import run_turn
from openhachimi_agent.storage.memory import load_message_history, save_message_history, start_new_session
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
        from openhachimi_agent.tools.utils import BoundedDict
        self.browser_manager = BrowserManager(config)
        self.process_manager = ProcessManager()
        self._session_states: BoundedDict[str, dict] = BoundedDict(100)
        self._artifact_records: BoundedDict[str, ArtifactRef] = BoundedDict(500)
        self._context_compressors: BoundedDict[str, Any] = BoundedDict(100)
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
        from openhachimi_agent.storage.memory import (
            create_session_id,
            load_latest_session_id,
            save_latest_session_id,
        )
        session_id = load_latest_session_id(self.config.memory_dir, role, scope)
        if not session_id or session_id == "legacy":
            session_id = create_session_id()
            save_latest_session_id(self.config.memory_dir, role, session_id, scope)
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
        session_id = start_new_session(self.config.memory_dir, role, scope)
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
        session_id = start_new_session(self.config.memory_dir, role, scope)
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
        return [StreamEventItem(type="system", text=response.output)]

    # ------------------------------------------------------------------ 上下文压缩

    async def compress_session(
        self,
        role: str,
        session_id: str,
        focus_topic: str = "",
        latest_scope: str | None = None,
    ) -> ChatResponse:
        """手动压缩指定会话的上下文历史(可带焦点主题)。"""
        role = self._normalize_role(role)
        actual_session_id, history = load_message_history(self.config.memory_dir, role, session_id, latest_scope)
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
        history_json = ModelMessagesTypeAdapter.dump_json(compressed)
        await asyncio.to_thread(
            save_message_history,
            self.config.memory_dir,
            role,
            actual_session_id,
            history_json,
            latest_scope,
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
        self._context_compressors[session_id] = compressor
        return compressor

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
                yield event

    async def stream_message(
        self,
        message: str,
        role: str | None = None,
        session_id: str | None = None,
        attachments: Sequence[AttachmentRef] | None = None,
    ) -> AsyncIterator[str]:
        async for event in self.stream_events(message, role, session_id, attachments=attachments):
            if event.type in {"text", "system"}:
                yield event.text
            elif event.type == "tool":
                yield f"\n[工具] {event.text}\n"
