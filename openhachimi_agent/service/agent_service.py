"""Agent 后台服务层。"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from openhachimi_agent.agent.factory import build_agent
from openhachimi_agent.content.roles import list_role_names
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.storage.memory import load_message_history, save_message_history, start_new_session
from openhachimi_agent.transport.api_models import AgentState, ChatResponse, CommandResponse, RolesResponse


logger = logging.getLogger(__name__)
_STREAM_DONE = object()


def _error_message(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return f"{exc.__class__.__name__}: {text}"
    return exc.__class__.__name__


def _text_from_stream_event(event: object) -> str:
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    return ""


def _summarize_tool_args(args: object, max_chars: int = 160) -> str:
    if args in (None, "", {}):
        return ""
    if isinstance(args, str):
        text = args
    else:
        try:
            text = json.dumps(args, ensure_ascii=False)
        except TypeError:
            text = str(args)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _status_from_stream_event(event: object) -> str:
    if isinstance(event, FunctionToolCallEvent):
        tool_name = event.part.tool_name
        args = _summarize_tool_args(event.part.args_as_dict())
        if args:
            return f"\n[工具] 正在调用 {tool_name}：{args}\n"
        return f"\n[工具] 正在调用 {tool_name}\n"

    if isinstance(event, FunctionToolResultEvent):
        result = event.result
        outcome = getattr(result, "outcome", "success")
        if outcome == "success":
            return f"[工具] {result.tool_name} 完成\n"
        return f"[工具] {result.tool_name} 结束：{outcome}\n"

    return ""


class AgentService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._agents = {}  # 缓存 (Agent 实例, 最后修改时间)，支持热重载
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        logger.info(
            "service initialized model=%s",
            self.config.model_name,
        )

    async def stop_session(self, session_id: str) -> CommandResponse:
        logger.info("stop requested for session_id=%s", session_id)
        if session_id in self._running_tasks:
            task = self._running_tasks[session_id]
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
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

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            # 防止长期运行导致无限制增长，保留最多 1000 个活跃 lock（LRU 策略）
            if len(self._session_locks) >= 1000:
                oldest_session = next(iter(self._session_locks))
                self._session_locks.pop(oldest_session, None)
            self._session_locks[session_id] = asyncio.Lock()
        else:
            # 将被访问的 lock 移到字典末尾，更新其“活跃度”
            lock = self._session_locks.pop(session_id)
            self._session_locks[session_id] = lock
            
        return self._session_locks[session_id]

    def _get_agent(self, role_name: str):
        # 计算依赖文件（角色文件和技能目录）的最新修改时间
        paths_to_check = [self.config.roles_dir / f"{role_name}.md"]
        paths_to_check.extend(self.config.skills_dirs)
        
        current_mtime = 0.0
        try:
            for path in paths_to_check:
                if not path.exists():
                    continue
                if path.is_file():
                    current_mtime = max(current_mtime, path.stat().st_mtime)
                elif path.is_dir():
                    for p in path.rglob('*'):
                        if p.is_file():
                            current_mtime = max(current_mtime, p.stat().st_mtime)
        except Exception as e:
            logger.debug("Failed to check mtime for agent dependencies: %s", e)

        cached = self._agents.get(role_name)
        if cached is None or cached[1] < current_mtime:
            if cached is not None:
                logger.info("rebuilding agent due to dependency updates role=%s", role_name)
            agent = build_agent(self.config, role_name)
            self._agents[role_name] = (agent, current_mtime)
            
        return self._agents[role_name][0]

    def state(self) -> AgentState:
        return AgentState(
            model=self.config.model_name,
            base_url=self.config.openai_base_url or None,
        )

    def list_roles(self) -> RolesResponse:
        logger.debug("listing roles roles_dir=%s", self.config.roles_dir)
        return RolesResponse(
            roles=list_role_names(self.config.roles_dir),
            current_role=self.config.default_role_name,
        )

    def latest_session(self, role_name: str | None = None) -> CommandResponse:
        role = role_name or self.config.default_role_name
        from openhachimi_agent.storage.memory import load_latest_session_id, create_session_id, save_latest_session_id
        session_id = load_latest_session_id(self.config.memory_dir, role)
        if not session_id or session_id == "legacy":
            session_id = create_session_id()
            save_latest_session_id(self.config.memory_dir, role, session_id)
            logger.info("no latest session found, created new session role=%s session_id=%s", role, session_id)
        else:
            logger.info("loaded latest session role=%s session_id=%s", role, session_id)
        
        return CommandResponse(
            message="已恢复上一次的对话上下文。",
            role=role,
            session_id=session_id,
        )

    def new_session(self, role_name: str | None = None) -> CommandResponse:
        role = role_name or self.config.default_role_name
        session_id = start_new_session(self.config.memory_dir, role)
        logger.info(
            "new session role=%s session_id=%s",
            role,
            session_id,
        )
        return CommandResponse(
            message="已保存上一段对话，并新建对话。",
            role=role,
            session_id=session_id,
        )

    def switch_role(self, role_name: str) -> CommandResponse:
        session_id = start_new_session(self.config.memory_dir, role_name)
        logger.info(
            "switched role to role=%s session_id=%s",
            role_name,
            session_id,
        )
        return CommandResponse(
            message=f"已切换到角色：{role_name}，并新建对话。",
            role=role_name,
            session_id=session_id,
        )

    async def send_message(self, message: str, role: str | None = None, session_id: str | None = None) -> ChatResponse:
        start_time = time.perf_counter()
        role = role or self.config.default_role_name
        agent = self._get_agent(role)
        
        actual_session_id, history = load_message_history(self.config.memory_dir, role, session_id)

        lock = self._get_session_lock(actual_session_id)

        async with lock:
            logger.info(
                "chat started role=%s session_id=%s message_chars=%d history_messages=%d stream=false",
                role,
                actual_session_id,
                len(message),
                len(history),
            )
            try:
                async def run_agent():
                    return await agent.run(message, message_history=history, deps=AgentDeps(config=self.config, session_id=actual_session_id))
                
                task = asyncio.create_task(run_agent())
                self._running_tasks[actual_session_id] = task
                try:
                    result = await task
                finally:
                    self._running_tasks.pop(actual_session_id, None)
            except asyncio.CancelledError:
                logger.info("chat cancelled role=%s session_id=%s stream=false", role, actual_session_id)
                return ChatResponse(output="【任务已被手动中断】", role=role, session_id=actual_session_id)
            except Exception:
                logger.exception(
                    "chat failed role=%s session_id=%s stream=false",
                    role,
                    actual_session_id,
                )
                raise
                
            new_history = list(result.all_messages())
            await asyncio.to_thread(
                save_message_history,
                self.config.memory_dir,
                role,
                actual_session_id,
                result.all_messages_json(),
            )
            logger.info(
                "chat finished role=%s session_id=%s output_chars=%d history_messages=%d duration_ms=%.0f stream=false",
                role,
                actual_session_id,
                len(str(result.output)),
                len(new_history),
                (time.perf_counter() - start_time) * 1000,
            )
            return ChatResponse(
                output=result.output,
                role=role,
                session_id=actual_session_id,
            )

    async def stream_message(self, message: str, role: str | None = None, session_id: str | None = None) -> AsyncIterator[str]:
        start_time = time.perf_counter()
        role = role or self.config.default_role_name
        agent = self._get_agent(role)
        
        actual_session_id, history = load_message_history(self.config.memory_dir, role, session_id)

        lock = self._get_session_lock(actual_session_id)

        async with lock:
            output_chars = 0
            chunk_count = 0
            first_chunk_ms: float | None = None
            last_chunk_preview = ""
            stream_queue: asyncio.Queue[str | object] = asyncio.Queue()
            result_holder: dict[str, object] = {}
            logger.info(
                "chat started role=%s session_id=%s message_chars=%d history_messages=%d stream=true",
                role,
                actual_session_id,
                len(message),
                len(history),
            )
    
            async def handle_stream_events(_ctx: object, stream: object) -> None:
                async for event in stream:  # type: ignore[attr-defined]
                    if chunk := _text_from_stream_event(event):
                        await stream_queue.put(chunk)
                    if status := _status_from_stream_event(event):
                        await stream_queue.put(status)
    
            async def run_agent() -> None:
                try:
                    result_holder["result"] = await asyncio.wait_for(
                        agent.run(
                            message,
                            message_history=history,
                            deps=AgentDeps(config=self.config, session_id=actual_session_id),
                            event_stream_handler=handle_stream_events,
                        ),
                        timeout=self.config.agent_timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    result_holder["error"] = TimeoutError(
                        "Agent 执行超时："
                        f"{self.config.agent_timeout_seconds}s 内没有完成。"
                        f"模型={self.config.model_name}，"
                        f"base_url={self.config.openai_base_url or '默认'}，"
                        f"role={role}，session_id={actual_session_id}。"
                        "常见原因：模型服务无响应、工具调用卡住、浏览器/网络代理不可用。"
                    )
                    logger.exception(
                        "chat timed out role=%s session_id=%s timeout_seconds=%d stream=true",
                        role,
                        actual_session_id,
                        self.config.agent_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    # Let CancelledError propagate natively
                    logger.info("chat stream cancelled role=%s session_id=%s", role, actual_session_id)
                    raise
                except Exception as exc:
                    result_holder["error"] = exc
                    logger.exception(
                        "chat failed role=%s session_id=%s stream=true",
                        role,
                        actual_session_id,
                    )
                finally:
                    await stream_queue.put(_STREAM_DONE)
    
            task = asyncio.create_task(run_agent())
            self._running_tasks[actual_session_id] = task
    
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(
                            stream_queue.get(),
                            timeout=self.config.stream_idle_timeout_seconds,
                        )
                    except asyncio.TimeoutError as exc:
                        elapsed_ms = (time.perf_counter() - start_time) * 1000
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                        logger.error(
                            "chat stream idle timeout role=%s session_id=%s idle_timeout_seconds=%d chunks=%d output_chars=%d duration_ms=%.0f last_chunk=%s",
                            role,
                            actual_session_id,
                            self.config.stream_idle_timeout_seconds,
                            chunk_count,
                            output_chars,
                            elapsed_ms,
                            last_chunk_preview,
                        )
                        last_chunk_detail = f"最后片段={last_chunk_preview!r}，" if last_chunk_preview else ""
                        raise TimeoutError(
                            "流式回复超时："
                            f"{self.config.stream_idle_timeout_seconds}s 内没有收到新的模型片段或工具状态。"
                            f"已收到片段数={chunk_count}，已输出字符数={output_chars}，"
                            f"{last_chunk_detail}"
                            f"模型={self.config.model_name}，"
                            f"base_url={self.config.openai_base_url or '默认'}，"
                            f"role={role}，session_id={actual_session_id}。"
                            "如果刚显示了几个字就停住，通常是后续模型请求、工具调用、浏览器或网络代理卡住。"
                        ) from exc
                    if item is _STREAM_DONE:
                        break
        
                    chunk = str(item)
                    compact_chunk = " ".join(chunk.split())
                    if len(compact_chunk) > 120:
                        compact_chunk = compact_chunk[:117] + "..."
                    if compact_chunk:
                        last_chunk_preview = compact_chunk
                    if first_chunk_ms is None:
                        first_chunk_ms = (time.perf_counter() - start_time) * 1000
                        logger.info(
                            "chat first chunk role=%s session_id=%s first_chunk_ms=%.0f chunk_chars=%d",
                            role,
                            actual_session_id,
                            first_chunk_ms,
                            len(chunk),
                        )
                    chunk_count += 1
                    output_chars += len(chunk)
                    yield chunk
        
                try:
                    await task
                except asyncio.CancelledError:
                    yield "\n\n【任务已被手动中断】"
                    return

                if error := result_holder.get("error"):
                    raise RuntimeError(f"Agent 调用失败：{_error_message(error)}") from error
        
                result = result_holder["result"]
                new_history = list(result.all_messages())  # type: ignore[attr-defined]
                history_json = result.all_messages_json()  # type: ignore[attr-defined]
                await asyncio.to_thread(
                    save_message_history,
                    self.config.memory_dir,
                    role,
                    actual_session_id,
                    history_json,
                )
        
                if not chunk_count:
                    output = str(result.output)  # type: ignore[attr-defined]
                    if output:
                        output_chars = len(output)
                        chunk_count = 1
                        logger.info(
                            "chat produced non-streamed output role=%s session_id=%s output_chars=%d",
                            role,
                            actual_session_id,
                            output_chars,
                        )
                        yield output
        
                logger.info(
                    "chat finished role=%s session_id=%s output_chars=%d chunks=%d first_chunk_ms=%s history_messages=%d duration_ms=%.0f stream=true",
                    role,
                    actual_session_id,
                    output_chars,
                    chunk_count,
                    f"{first_chunk_ms:.0f}" if first_chunk_ms is not None else None,
                    len(new_history),
                    (time.perf_counter() - start_time) * 1000,
                )
            finally:
                self._running_tasks.pop(actual_session_id, None)
