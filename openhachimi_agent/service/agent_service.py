"""Agent 后台服务层。"""

import asyncio
import contextlib
import json
import logging
import time
import weakref
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

from openhachimi_agent.agent.factory import (
    build_continuation_agent,
    build_executor_agent,
    build_planner_agent,
    build_router_agent,
    build_scheduled_executor_agent,
    build_self_critique_agent,
)
from openhachimi_agent.content.roles import list_role_names, load_role_content
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.core.identifiers import validate_latest_scope, validate_role_name, validate_session_id
from openhachimi_agent.core.redaction import redact_exception, redact_text
from openhachimi_agent.service.agent_runtime.context import (
    AgentRunContext,
    complete_current_plan,
    fail_current_plan,
    has_active_todos,
    mark_turn_finished,
    mark_turn_started,
    suspend_current_plan,
)
from openhachimi_agent.service.agent_runtime.executor import execute_task, message_with_attachments
from openhachimi_agent.service.agent_runtime.planner import needs_planning, run_planner
from openhachimi_agent.service.agent_runtime.router import resolve_task_frame, should_route_message
from openhachimi_agent.service.agent_runtime.streaming import (
    STREAM_DONE,
    OperationStalledError,
    StreamEventItem,
    StreamStats,
    build_stream_event_handler,
    consume_stream_queue,
    system_stream_event,
)
from openhachimi_agent.memory.capture import capture_turn_memories
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.recall import recall_memories
from openhachimi_agent.storage.memory import load_message_history, save_message_history, start_new_session
from openhachimi_agent.transport.api_models import AgentState, ArtifactRef, AttachmentRef, ChatResponse, CommandResponse, RolesResponse


logger = logging.getLogger(__name__)
AGENT_DEPENDENCY_MTIME_TTL_SECONDS = 2.0
PRIORITY_STOP_COMMANDS = {"/stop", "停止"}
PRIORITY_NEW_SESSION_COMMANDS = {"/new", "新对话"}

# result_holder 中的信号键 → 附加到输出的提示文案，流式与非流式路径共用。
SIGNAL_LABELS: tuple[tuple[str, str], ...] = (
    ("final_verification_signal", "[最终验证未通过] 当前执行结果仍缺少完成证据："),
    ("self_critique_signal", "[自检未通过] 当前最终回复可能仍未完全满足用户意图："),
)


def _error_message(exc: BaseException) -> str:
    return redact_exception(exc)



class AgentService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._agents = {}  # 缓存 (Agent 实例, 最后修改时间)，支持热重载
        self._agent_dependency_mtime_cache: tuple[float, float] | None = None
        self._mcp_toolsets = []
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
        logger.info(
            "service initialized model=%s",
            self.config.model_name,
        )

    async def start(self) -> None:
        """启动后台服务需要长期维护的资源，如 MCP 连接。"""
        await self._reload_mcp_toolsets(force=True)

    async def stop(self) -> None:
        """关闭服务资源。"""
        try:
            await self._mcp_stack.aclose()
        except Exception:
            logger.exception("Error closing MCP toolsets")

    def _get_mcp_config_signature(self) -> tuple[float, int] | None:
        mcp_file = self.config.user_dir / "mcp-servers.json"
        try:
            if not mcp_file.exists() or not mcp_file.is_file():
                return None
            stat = mcp_file.stat()
            return (stat.st_mtime, stat.st_size)
        except Exception as exc:
            logger.debug("Failed to check mcp-servers.json signature: %s", exc)
            return self._mcp_config_signature

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

            from openhachimi_agent.core.config import load_mcp_config
            from openhachimi_agent.tools.mcp import load_mcp_toolsets

            logger.info("reloading MCP toolsets signature_changed=%s", signature != self._mcp_config_signature)
            runtime_config = replace(self.config, mcp=load_mcp_config(self.config.user_dir))
            new_stack = contextlib.AsyncExitStack()
            connected_toolsets = []
            errors: list[str] = []

            try:
                for ts in load_mcp_toolsets(runtime_config):
                    try:
                        await new_stack.enter_async_context(ts)
                    except Exception as exc:
                        message = _error_message(exc)
                        errors.append(message)
                        logger.exception("Failed to start MCP toolset connection")
                    else:
                        connected_toolsets.append(ts)

                old_stack = self._mcp_stack
                self.config = runtime_config
                self._mcp_stack = new_stack
                self._mcp_toolsets = connected_toolsets
                self._mcp_config_signature = signature
                self._mcp_errors = errors
                self._agents.clear()
                await old_stack.aclose()
                logger.info(
                    "MCP toolsets reloaded connected=%d errors=%d",
                    len(connected_toolsets),
                    len(errors),
                )
            except Exception:
                await new_stack.aclose()
                raise

    def _normalize_role(self, role_name: str | None) -> str:
        role = validate_role_name(role_name or self.config.default_role_name)
        return role

    def _normalize_session_id(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        return validate_session_id(session_id, allow_legacy=False)

    def _validate_role_exists(self, role_name: str) -> None:
        if role_name == self.config.default_role_name and not list_role_names(self.config.roles_dir):
            return
        load_role_content(self.config.roles_dir, role_name)

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

    def _priority_command(self, message: str) -> str | None:
        command = message.strip()
        if command in PRIORITY_STOP_COMMANDS:
            return "stop"
        if command in PRIORITY_NEW_SESSION_COMMANDS:
            return "new"
        return None

    def _latest_scope_from_context(self, channel_context: dict[str, object] | None) -> str | None:
        if not channel_context:
            return None
        scope = channel_context.get("session_scope_key")
        if not scope:
            return None
        return validate_latest_scope(str(scope))

    def _resolve_priority_session(self, role: str | None, session_id: str | None, latest_scope: str | None = None) -> tuple[str, str]:
        resolved_role = self._normalize_role(role)
        resolved_session_id = self._normalize_session_id(session_id)
        if resolved_session_id:
            return resolved_role, resolved_session_id
        latest = self.latest_session(resolved_role, latest_scope=latest_scope)
        return latest.role, latest.session_id

    async def _handle_priority_command_response(
        self,
        message: str,
        role: str | None,
        session_id: str | None,
        channel_context: dict[str, object] | None = None,
    ) -> ChatResponse | None:
        command = self._priority_command(message)
        if command is None:
            return None
        latest_scope = self._latest_scope_from_context(channel_context)
        resolved_role, resolved_session_id = self._resolve_priority_session(role, session_id, latest_scope)
        if command == "stop":
            resp = await self.stop_session(resolved_session_id)
            return ChatResponse(output=resp.message, role=resolved_role, session_id=resolved_session_id)

        await self.stop_session(resolved_session_id)
        resp = self.new_session(resolved_role, latest_scope=latest_scope)
        return ChatResponse(output=resp.message, role=resp.role, session_id=resp.session_id)

    async def _handle_priority_command_events(
        self,
        message: str,
        role: str | None,
        session_id: str | None,
        channel_context: dict[str, object] | None = None,
    ) -> list[StreamEventItem] | None:
        response = await self._handle_priority_command_response(message, role, session_id, channel_context)
        if response is None:
            return None
        return [StreamEventItem(type="system", text=response.output)]

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _get_agent_dependency_mtime(self, role_name: str) -> float:
        now = time.monotonic()
        if self._agent_dependency_mtime_cache is not None:
            checked_at, cached_mtime = self._agent_dependency_mtime_cache
            if now - checked_at < AGENT_DEPENDENCY_MTIME_TTL_SECONDS:
                return cached_mtime

        current_mtime = 0.0
        paths_to_check = [self.config.roles_dir / f"{role_name}.md"]
        try:
            for path in paths_to_check:
                if path.exists() and path.is_file():
                    current_mtime = max(current_mtime, path.stat().st_mtime)

            for skills_dir in self.config.skills_dirs:
                if not skills_dir.exists() or not skills_dir.is_dir():
                    continue
                for skill_file in skills_dir.rglob("SKILL.md"):
                    if skill_file.is_file():
                        current_mtime = max(current_mtime, skill_file.stat().st_mtime)
        except Exception as exc:
            logger.debug("Failed to check mtime for agent dependencies: %s", exc)

        self._agent_dependency_mtime_cache = (now, current_mtime)
        return current_mtime

    def _get_agent(self, role_name: str, agent_type: str = "executor"):
        cache_key = f"{role_name}:{agent_type}"
        current_mtime = self._get_agent_dependency_mtime(role_name)

        cached = self._agents.get(cache_key)
        if cached is None or cached[1] < current_mtime:
            if cached is not None:
                logger.info("rebuilding %s agent due to dependency updates role=%s", agent_type, role_name)
                
            if agent_type == "router":
                agent = build_router_agent(self.config)
            elif agent_type == "continuation":
                agent = build_continuation_agent(self.config)
            elif agent_type == "self_critique":
                agent = build_self_critique_agent(self.config)
            elif agent_type == "planner":
                agent = build_planner_agent(self.config, role_name, mcp_toolsets=self._mcp_toolsets)
            elif agent_type == "scheduled_executor":
                agent = build_scheduled_executor_agent(self.config, role_name, mcp_toolsets=self._mcp_toolsets)
            else:
                agent = build_executor_agent(self.config, role_name, mcp_toolsets=self._mcp_toolsets)
                
            self._agents[cache_key] = (agent, current_mtime)
            
        return self._agents[cache_key][0]

    def register_artifacts(self, artifacts: list[ArtifactRef]) -> None:
        for artifact in artifacts:
            self._artifact_records[artifact.id] = artifact

    def get_artifact(self, artifact_id: str) -> ArtifactRef | None:
        return self._artifact_records.get(artifact_id)

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
        from openhachimi_agent.storage.memory import load_latest_session_id, create_session_id, save_latest_session_id
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
            f"🤖 模型：{self.config.model_name}",
        ]
        if self.config.openai_base_url:
            lines.append(f"🌐 模型服务：{self.config.openai_base_url}")
        lines.extend([
            f"🎭 角色：{role}",
            f"🧩 会话：{session_id}",
            "",
            "💬 直接输入内容并回车，即可继续对话。",
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
            message=f"已切换到角色：{role}，并新建对话。",
            role=role,
            session_id=session_id,
        )

    async def _run_with_session(
        self,
        message: str,
        role: str | None,
        session_id: str | None,
        stream: bool,
        attachments: Sequence[AttachmentRef] | None = None,
        run_mode: str = "interactive",
        channel_context: dict[str, object] | None = None,
        scheduler_context: dict[str, object] | None = None,
        channel: str | None = None,
        delivery_target: dict[str, object] | None = None,
    ) -> AsyncIterator[object]:
        start_time = time.perf_counter()
        role = self._normalize_role(role)
        session_id = self._normalize_session_id(session_id)
        self._validate_role_exists(role)
        latest_scope = validate_latest_scope(str(channel_context.get("session_scope_key"))) if channel_context and channel_context.get("session_scope_key") else None
        attachment_list = list(attachments or [])
        effective_message = message_with_attachments(message, attachment_list)

        channel_context_data = dict(channel_context or {})
        if not channel_context_data:
            channel_context_data = {"type": channel or "local", "platform": channel or "local"}
            if delivery_target:
                channel_context_data.update(delivery_target)
        channel_name = str(channel_context_data.get("type") or channel_context_data.get("platform") or "local")

        actual_session_id, history = load_message_history(self.config.memory_dir, role, session_id, latest_scope)
        lock = self._get_session_lock(actual_session_id)

        async with lock:
            logger.info(
                "chat started role=%s session_id=%s message_chars=%d history_messages=%d attachment_count=%d stream=%s",
                role,
                actual_session_id,
                len(message),
                len(history),
                len(attachment_list),
                str(stream).lower(),
            )
            await self._maybe_reload_mcp_toolsets()

            if actual_session_id not in self._session_states:
                self._session_states[actual_session_id] = {}
            session_state = self._session_states[actual_session_id]
            session_state["turn_artifacts"] = []
            memory_scope = MemoryScope(
                tenant_id="local",
                user_id="local",
                role_name=role,
                session_id=actual_session_id,
                channel=channel_name,
            )
            memory_context = recall_memories(self.config, memory_scope, effective_message)
            session_state["memory_context"] = memory_context
            deps = AgentDeps(
                config=self.config,
                session_id=actual_session_id,
                browser_manager=self.browser_manager,
                process_manager=self.process_manager,
                session_state=session_state,
                memory_scope=memory_scope,
                memory_context=memory_context,
                run_mode=run_mode,
                channel_context=channel_context_data,
                scheduler_context=dict(scheduler_context or {}),
            )
            stream_queue: asyncio.Queue[StreamEventItem | object] = asyncio.Queue()
            stream_stats = StreamStats()
            result_holder: dict[str, object] = {}
            ctx = AgentRunContext(
                config=self.config,
                role=role,
                session_id=actual_session_id,
                message=message,
                attachments=attachment_list,
                history=history,
                deps=deps,
                session_state=session_state,
                stream=stream,
                stream_queue=stream_queue,
            )
            ctx.stream_event_handler = build_stream_event_handler(stream_queue, ctx.operation_state)

            async def refresh_mcp_config() -> None:
                await self._maybe_reload_mcp_toolsets()
                ctx.config = self.config
                deps.config = self.config

            should_route = await should_route_message(ctx, self._get_agent)

            async def run_agent() -> None:
                mark_turn_started(session_state)
                try:
                    if should_route:
                        await refresh_mcp_config()
                        task_frame = await resolve_task_frame(ctx, self._get_agent)
                        session_state["task_frame"] = task_frame.model_dump(mode="json")
                        if needs_planning(task_frame):
                            await refresh_mcp_config()
                            await run_planner(ctx, task_frame, self._get_agent)

                    await refresh_mcp_config()
                    outcome = await execute_task(ctx, self._get_agent)
                    result_holder["result"] = outcome.result
                    if outcome.final_verification_signal:
                        result_holder["final_verification_signal"] = outcome.final_verification_signal
                        if has_active_todos(session_state):
                            suspend_current_plan(
                                session_state,
                                reason="final_verification_failed",
                                detail=outcome.final_verification_signal,
                                deps=deps,
                            )
                        else:
                            fail_current_plan(
                                session_state,
                                reason="final_verification_failed",
                                detail=outcome.final_verification_signal,
                            )
                    elif outcome.self_critique_signal:
                        result_holder["self_critique_signal"] = outcome.self_critique_signal
                        if has_active_todos(session_state):
                            suspend_current_plan(
                                session_state,
                                reason="self_critique_failed",
                                detail=outcome.self_critique_signal,
                                deps=deps,
                            )
                        else:
                            fail_current_plan(
                                session_state,
                                reason="self_critique_failed",
                                detail=outcome.self_critique_signal,
                            )
                    else:
                        complete_current_plan(session_state)
                except asyncio.TimeoutError as exc:
                    if has_active_todos(session_state):
                        suspend_current_plan(
                            session_state,
                            reason="operation_timeout",
                            detail=str(exc),
                            deps=deps,
                        )
                    else:
                        fail_current_plan(session_state, reason="operation_timeout", detail=str(exc))
                    if stream:
                        result_holder["error"] = TimeoutError(
                            "Agent 执行超时："
                            f"{self.config.agent_timeout_seconds}s 内没有完成。"
                            f"模型={self.config.model_name}，"
                            f"base_url={redact_text(self.config.openai_base_url or '默认')}，"
                            f"role={role}，session_id={actual_session_id}。"
                            "常见原因：模型服务无响应、工具调用卡住、浏览器/网络代理不可用。"
                        )
                        logger.exception(
                            "chat timed out role=%s session_id=%s timeout_seconds=%d stream=true",
                            role,
                            actual_session_id,
                            self.config.agent_timeout_seconds,
                        )
                    else:
                        result_holder["error"] = exc
                        logger.exception(
                            "chat timed out role=%s session_id=%s stream=false",
                            role,
                            actual_session_id,
                        )
                except asyncio.CancelledError:
                    if has_active_todos(session_state):
                        suspend_current_plan(
                            session_state,
                            reason="cancelled",
                            detail="agent task cancelled",
                            deps=deps,
                        )
                    else:
                        fail_current_plan(session_state, reason="cancelled", detail="agent task cancelled")
                    logger.info(
                        "chat stream cancelled role=%s session_id=%s" if stream else "chat cancelled role=%s session_id=%s stream=false",
                        role,
                        actual_session_id,
                    )
                    raise
                except Exception as exc:
                    if has_active_todos(session_state):
                        suspend_current_plan(
                            session_state,
                            reason="error",
                            detail=redact_exception(exc),
                            deps=deps,
                        )
                    else:
                        fail_current_plan(session_state, reason="error", detail=redact_exception(exc))
                    result_holder["error"] = exc
                    logger.exception(
                        "chat failed role=%s session_id=%s stream=%s",
                        role,
                        actual_session_id,
                        str(stream).lower(),
                    )
                finally:
                    mark_turn_finished(session_state)
                    if stream:
                        with contextlib.suppress(asyncio.CancelledError):
                            await stream_queue.put(STREAM_DONE)

            task = asyncio.create_task(run_agent())
            self._running_tasks[actual_session_id] = task

            try:
                if stream:
                    try:
                        async for event in consume_stream_queue(
                            stream_queue=stream_queue,
                            task=task,
                            config=self.config,
                            role=role,
                            session_id=actual_session_id,
                            start_time=start_time,
                            stats=stream_stats,
                            operation_state=ctx.operation_state,
                        ):
                            yield event
                    except OperationStalledError as exc:
                        stalled_detail = {"operation": exc.operation, "stalled_for": exc.stalled_for, "timeout": exc.timeout}
                        if has_active_todos(session_state):
                            suspend_current_plan(
                                session_state,
                                reason="operation_stalled",
                                detail=stalled_detail,
                                deps=deps,
                            )
                            yield system_stream_event(
                                "\n\n[System] 当前任务已暂停："
                                f"{exc} 旧计划已挂起，不会影响下一轮对话；"
                                "如需恢复，请明确说明“继续刚才的任务”。"
                            )
                        else:
                            fail_current_plan(session_state, reason="operation_stalled", detail=stalled_detail)
                            yield system_stream_event(f"\n\n[System] 当前任务已失败：{exc} 未生成可恢复计划，下一轮将重新理解用户请求。")
                        return

                    try:
                        await task
                    except asyncio.CancelledError:
                        if task.cancelled():
                            yield system_stream_event("\n\n【任务已被手动中断】")
                            return
                        raise

                    if error := result_holder.get("error"):
                        raise RuntimeError(f"Agent 调用失败：{_error_message(error)}") from error
                    for signal_key, signal_label in SIGNAL_LABELS:
                        if signal_value := result_holder.get(signal_key):
                            yield system_stream_event(
                                f"\n\n{signal_label}{json.dumps(signal_value, ensure_ascii=False)}"
                            )
                    turn_artifacts = [
                        artifact for artifact in session_state.get("turn_artifacts", [])
                        if isinstance(artifact, ArtifactRef)
                    ]
                    self.register_artifacts(turn_artifacts)
                    seen_artifacts: set[str] = set()
                    for artifact in turn_artifacts:
                        if artifact.id in seen_artifacts:
                            continue
                        seen_artifacts.add(artifact.id)
                        yield StreamEventItem(
                            type="artifact",
                            text=f"已生成文件：{artifact.filename}",
                            artifact=artifact,
                            counted_as_output=False,
                        )
                else:
                    try:
                        await task
                    except asyncio.CancelledError:
                        if task.cancelled():
                            yield ChatResponse(output="【任务已被手动中断】", role=role, session_id=actual_session_id)
                            return
                        raise

                    if error := result_holder.get("error"):
                        raise error

                result = result_holder["result"]
                turn_artifacts = [
                    artifact for artifact in session_state.get("turn_artifacts", [])
                    if isinstance(artifact, ArtifactRef)
                ]
                self.register_artifacts(turn_artifacts)
                new_history = list(result.all_messages())  # type: ignore[attr-defined]
                history_json = result.all_messages_json()  # type: ignore[attr-defined]

                await asyncio.to_thread(
                    save_message_history,
                    self.config.memory_dir,
                    role,
                    actual_session_id,
                    history_json,
                    latest_scope,
                )
                capture_args = (
                    self.config,
                    memory_scope,
                    effective_message,
                    str(result.output),  # type: ignore[attr-defined]
                )
                capture_kwargs = {
                    "task_frame": session_state.get("task_frame") if isinstance(session_state.get("task_frame"), dict) else None,
                    "memory_context_ids": memory_context.ids,
                    "duration_ms": int((time.perf_counter() - start_time) * 1000),
                }
                if self.config.memory.capture.async_enabled:
                    async def _capture_memory_background() -> None:
                        try:
                            await asyncio.to_thread(capture_turn_memories, *capture_args, **capture_kwargs)
                        except Exception:
                            logger.exception("memory capture failed role=%s session_id=%s", role, actual_session_id)

                    asyncio.create_task(_capture_memory_background())
                else:
                    await asyncio.to_thread(capture_turn_memories, *capture_args, **capture_kwargs)

                if stream:
                    if not stream_stats.chunk_count:
                        output = str(result.output)  # type: ignore[attr-defined]
                        if output:
                            stream_stats.output_chars = len(output)
                            stream_stats.chunk_count = 1
                            logger.info(
                                "chat produced non-streamed output role=%s session_id=%s output_chars=%d",
                                role,
                                actual_session_id,
                                stream_stats.output_chars,
                            )
                            yield StreamEventItem(type="text", text=output)

                    logger.info(
                        "chat finished role=%s session_id=%s output_chars=%d chunks=%d first_chunk_ms=%s history_messages=%d duration_ms=%.0f stream=true",
                        role,
                        actual_session_id,
                        stream_stats.output_chars,
                        stream_stats.chunk_count,
                        f"{stream_stats.first_chunk_ms:.0f}" if stream_stats.first_chunk_ms is not None else None,
                        len(new_history),
                        (time.perf_counter() - start_time) * 1000,
                    )
                else:
                    logger.info(
                        "chat finished role=%s session_id=%s output_chars=%d history_messages=%d duration_ms=%.0f stream=false",
                        role,
                        actual_session_id,
                        len(str(result.output)),  # type: ignore[attr-defined]
                        len(new_history),
                        (time.perf_counter() - start_time) * 1000,
                    )
                    output = result.output  # type: ignore[attr-defined]
                    for signal_key, signal_label in SIGNAL_LABELS:
                        if signal_value := result_holder.get(signal_key):
                            output = f"{output}\n\n{signal_label}{json.dumps(signal_value, ensure_ascii=False)}"
                    yield ChatResponse(
                        output=output,
                        role=role,
                        session_id=actual_session_id,
                        artifacts=turn_artifacts,
                    )
            finally:
                self._running_tasks.pop(actual_session_id, None)
                if not task.done():
                    task.cancel()

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
        priority_response = await self._handle_priority_command_response(message, role, session_id, channel_context)
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
        raise RuntimeError("No result returned from _run_with_session")

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
        priority_events = await self._handle_priority_command_events(message, role, session_id, channel_context)
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
