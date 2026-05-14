"""Agent 后台服务层。"""

import asyncio
import contextlib
import json
import logging
import time
import weakref
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

from openhachimi_agent.agent.factory import build_executor_agent, build_planner_agent, build_router_agent
from openhachimi_agent.agent.execution import get_final_verification_signal, get_ledger_length, get_replan_signal
from openhachimi_agent.agent.intent import build_task_frame, classify_intent_heuristic, coerce_task_frame
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
        self._session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        from openhachimi_agent.service.browser import BrowserManager
        from openhachimi_agent.service.process import ProcessManager
        from openhachimi_agent.tools.utils import BoundedDict
        self.browser_manager = BrowserManager(config)
        self.process_manager = ProcessManager()
        self._session_states: BoundedDict[str, dict] = BoundedDict(100)
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
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _get_agent(self, role_name: str, agent_type: str = "executor"):
        cache_key = f"{role_name}:{agent_type}"
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

        cached = self._agents.get(cache_key)
        if cached is None or cached[1] < current_mtime:
            if cached is not None:
                logger.info("rebuilding %s agent due to dependency updates role=%s", agent_type, role_name)
                
            if agent_type == "router":
                agent = build_router_agent(self.config)
            elif agent_type == "planner":
                agent = build_planner_agent(self.config, role_name)
            else:
                agent = build_executor_agent(self.config, role_name)
                
            self._agents[cache_key] = (agent, current_mtime)
            
        return self._agents[cache_key][0]

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

    async def _run_with_session(self, message: str, role: str | None, session_id: str | None, stream: bool) -> AsyncIterator[object]:
        start_time = time.perf_counter()
        role = role or self.config.default_role_name
        
        actual_session_id, history = load_message_history(self.config.memory_dir, role, session_id)
        lock = self._get_session_lock(actual_session_id)

        async with lock:
            logger.info(
                "chat started role=%s session_id=%s message_chars=%d history_messages=%d stream=%s",
                role,
                actual_session_id,
                len(message),
                len(history),
                str(stream).lower(),
            )

            stream_queue: asyncio.Queue[str | object] = asyncio.Queue()
            result_holder: dict[str, object] = {}

            async def handle_stream_events(_ctx: object, stream_event: object) -> None:
                async for event in stream_event:  # type: ignore[attr-defined]
                    if chunk := _text_from_stream_event(event):
                        await stream_queue.put(chunk)
                    if status := _status_from_stream_event(event):
                        await stream_queue.put(status)

            async def run_agent() -> None:
                try:
                    if actual_session_id not in self._session_states:
                        self._session_states[actual_session_id] = {}
                    session_state = self._session_states[actual_session_id]
                    deps = AgentDeps(
                        config=self.config, 
                        session_id=actual_session_id, 
                        browser_manager=self.browser_manager,
                        process_manager=self.process_manager,
                        session_state=session_state
                    )

                    # 1. 检查是否存在正在进行的 TODO
                    has_active_todos = False
                    if "todo_state" in session_state:
                        todo_state = session_state["todo_state"]
                        if todo_state.is_active and todo_state.tasks:
                            if any(t.status != "done" for t in todo_state.tasks.values()):
                                has_active_todos = True
                    
                    # 2. 如果没有活动的 TODO，且是一句新的指令，通过 Router 判断复杂度
                    if not has_active_todos and len(history) % 2 == 0:  # 假设新的一轮，只有在没有活动 TODO 时才做 router 判断
                        task_frame = None
                        try:
                            router_agent = self._get_agent(role, "router")
                            router_result = await router_agent.run(f"指令：{message}")
                            router_data = getattr(router_result, "data", getattr(router_result, "output", None))
                            task_frame = coerce_task_frame(router_data, message)
                        except Exception as router_e:
                            decision = classify_intent_heuristic(message)
                            if not (decision.task_kind == "browser" and decision.target_urls and decision.risk != "high"):
                                decision.requires_plan = True
                            decision.rationale = f"router failed: {router_e.__class__.__name__}"
                            task_frame = build_task_frame(message, decision)
                            logger.warning("Router failed: %s. Falling back to conservative planning.", router_e)

                        logger.info(
                            "Task frame kind=%s complexity=%s risk=%s confidence=%.2f requires_plan=%s autonomy=%s targets=%s rationale=%s",
                            task_frame.task_kind,
                            task_frame.complexity,
                            task_frame.risk,
                            task_frame.confidence,
                            task_frame.requires_plan,
                            task_frame.allowed_autonomy,
                            [entity.value for entity in task_frame.target_entities],
                            task_frame.rationale,
                        )

                        session_state["task_frame"] = task_frame.model_dump(mode="json")

                        # 3. 如果是复杂/高风险/低置信任务，强制进入 Planner 进行规划
                        if task_frame.requires_plan or task_frame.confidence < 0.5:
                            planner_agent = self._get_agent(role, "planner")
                            if stream:
                                await stream_queue.put(
                                    "\n\n[System] 检测到任务需要前置规划，正在进行只读调研与计划拆解...\n"
                                )
                            heartbeat_task: asyncio.Task | None = None

                            async def planner_heartbeat() -> None:
                                interval = max(5, min(20, self.config.stream_idle_timeout_seconds // 2))
                                while True:
                                    await asyncio.sleep(interval)
                                    await stream_queue.put("\n[System] 规划仍在进行中，等待模型返回计划...\n")

                            if stream:
                                heartbeat_task = asyncio.create_task(planner_heartbeat())
                            try:
                                planner_result = await planner_agent.run(
                                    "请针对以下 TaskFrame 制定执行计划。\n"
                                    "你只需要制定计划（使用 create_todos），不需要自己执行任何调研或搜索。\n"
                                    "Executor 拥有浏览器、文件操作、命令行、web_fetch、web_search 等全部工具，请基于对这些工具能力的理解来规划步骤。\n"
                                    "如果用户提供了明确的 URL 或文件路径，计划应从直接访问该目标开始。\n"
                                    "TaskFrame 是任务契约：计划必须继承 goal、target_entities、invariants，不得扩大或替换目标。\n"
                                    "计划中的每个任务应尽量包含 description、depends_on、success_criteria、verification、risk_level；"
                                    "如果某一步只允许特定工具，可填写 allowed_tools。\n"
                                    f"TaskFrame：{task_frame.model_dump_json(ensure_ascii=False)}\n"
                                    f"用户原始任务：{message}",
                                    message_history=history,
                                    deps=deps
                                )
                            finally:
                                if heartbeat_task is not None:
                                    heartbeat_task.cancel()
                                    with contextlib.suppress(asyncio.CancelledError):
                                        await heartbeat_task
                            # 将规划历史记录附加到执行前
                            history.extend(planner_result.all_messages())
                            
                    # 4. 最后交给 Executor Agent 执行
                    executor_agent = self._get_agent(role, "executor")
                    task_frame_payload = session_state.get("task_frame")
                    executor_message = message
                    if task_frame_payload:
                        executor_message = (
                            "请执行以下用户任务。必须遵守 TaskFrame 中的 goal、target_entities、invariants、allowed_autonomy 和 replan_triggers；"
                            "如果工具观察结果与 TaskFrame 冲突，应停止当前动作并重新校准目标。\n"
                            f"TaskFrame：{json.dumps(task_frame_payload, ensure_ascii=False)}\n"
                            f"用户原始任务：{message}"
                        )
                    
                    kwargs = {
                        "message_history": history,
                        "deps": deps,
                    }

                    async def run_executor_once(run_message: str):
                        if stream:
                            kwargs["event_stream_handler"] = handle_stream_events
                            return await asyncio.wait_for(
                                executor_agent.run(run_message, **kwargs),
                                timeout=self.config.agent_timeout_seconds,
                            )
                        return await executor_agent.run(run_message, **kwargs)

                    async def replan_after_execution_signal(signal: dict[str, object]) -> None:
                        planner_agent = self._get_agent(role, "planner")
                        if stream:
                            await stream_queue.put("\n\n[System] 执行遇到偏差，正在根据执行记录修订计划...\n")
                        planner_result = await planner_agent.run(
                            "Executor 在执行时触发了 TaskFrame 偏差或工具失败。请基于 TaskFrame、当前 TODO 和 execution ledger 摘要修订计划。\n"
                            "要求：保持 TaskFrame 的 goal、target_entities、invariants 不变；不要扩大任务目标；"
                            "如果原计划错误，请调用 create_todos 重建一个更窄、更可执行的计划。\n"
                            f"TaskFrame：{json.dumps(task_frame_payload or {}, ensure_ascii=False)}\n"
                            f"Execution ledger replan signal：{json.dumps(signal, ensure_ascii=False)}\n"
                            f"用户原始任务：{message}",
                            message_history=history,
                            deps=deps,
                        )
                        history.extend(planner_result.all_messages())

                    ledger_start_seq = get_ledger_length(session_state)
                    if stream:
                        kwargs["event_stream_handler"] = handle_stream_events
                    try:
                        result = await run_executor_once(executor_message)
                    except Exception:
                        signal = get_replan_signal(session_state, ledger_start_seq)
                        if signal and not session_state.get("replan_attempted"):
                            session_state["replan_attempted"] = True
                            logger.info(
                                "triggering replan role=%s session_id=%s signal=%s",
                                role,
                                actual_session_id,
                                json.dumps(signal, ensure_ascii=False),
                            )
                            await replan_after_execution_signal(signal)
                            retry_message = (
                                "请根据刚刚修订后的计划继续执行用户任务。必须遵守 TaskFrame 和新的 TODO；"
                                "如果再次遇到同类偏差，请停止并向用户说明阻塞原因。\n"
                                f"TaskFrame：{json.dumps(task_frame_payload or {}, ensure_ascii=False)}\n"
                                f"用户原始任务：{message}"
                            )
                            result = await run_executor_once(retry_message)
                        else:
                            raise

                    verification_signal = get_final_verification_signal(session_state)
                    if verification_signal and not session_state.get("final_verification_repair_attempted"):
                        session_state["final_verification_repair_attempted"] = True
                        logger.info(
                            "triggering final verification repair role=%s session_id=%s signal=%s",
                            role,
                            actual_session_id,
                            json.dumps(verification_signal, ensure_ascii=False),
                        )
                        if stream:
                            await stream_queue.put("\n\n[System] 最终验证发现任务尚未满足，正在补齐缺口...\n")
                        repair_message = (
                            "最终验证器发现当前执行结果尚不足以宣称完成。请只补齐验证器指出的缺口，"
                            "继续严格遵守 TaskFrame、TODO 和执行记录；完成后必须更新 TODO 或提供足够证据。\n"
                            f"TaskFrame：{json.dumps(task_frame_payload or {}, ensure_ascii=False)}\n"
                            f"Final verification signal：{json.dumps(verification_signal, ensure_ascii=False)}\n"
                            f"用户原始任务：{message}"
                        )
                        result = await run_executor_once(repair_message)
                        verification_signal = get_final_verification_signal(session_state)

                    if verification_signal:
                        result_holder["final_verification_signal"] = verification_signal
                    result_holder["result"] = result
                except asyncio.TimeoutError as exc:
                    if stream:
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
                    else:
                        result_holder["error"] = exc
                        logger.exception(
                            "chat timed out role=%s session_id=%s stream=false",
                            role,
                            actual_session_id,
                        )
                except asyncio.CancelledError:
                    # Let CancelledError propagate natively
                    logger.info("chat stream cancelled role=%s session_id=%s" if stream else "chat cancelled role=%s session_id=%s stream=false", role, actual_session_id)
                    raise
                except Exception as exc:
                    result_holder["error"] = exc
                    logger.exception(
                        "chat failed role=%s session_id=%s stream=%s",
                        role,
                        actual_session_id,
                        str(stream).lower(),
                    )
                finally:
                    if stream:
                        await stream_queue.put(_STREAM_DONE)

            task = asyncio.create_task(run_agent())
            self._running_tasks[actual_session_id] = task

            try:
                if stream:
                    output_chars = 0
                    chunk_count = 0
                    first_chunk_ms: float | None = None
                    last_chunk_preview = ""
                    
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
                        if task.cancelled():
                            yield "\n\n【任务已被手动中断】"
                            return
                        raise

                    if error := result_holder.get("error"):
                        raise RuntimeError(f"Agent 调用失败：{_error_message(error)}") from error
                    if final_signal := result_holder.get("final_verification_signal"):
                        yield (
                            "\n\n[最终验证未通过] 当前执行结果仍缺少完成证据："
                            f"{json.dumps(final_signal, ensure_ascii=False)}"
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

                # Common history saving
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

                if stream:
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
                    if final_signal := result_holder.get("final_verification_signal"):
                        output = (
                            f"{output}\n\n[最终验证未通过] 当前执行结果仍缺少完成证据："
                            f"{json.dumps(final_signal, ensure_ascii=False)}"
                        )
                    yield ChatResponse(
                        output=output,
                        role=role,
                        session_id=actual_session_id,
                    )

            finally:
                self._running_tasks.pop(actual_session_id, None)
                if not task.done():
                    task.cancel()

    async def send_message(self, message: str, role: str | None = None, session_id: str | None = None) -> ChatResponse:
        async for result in self._run_with_session(message, role, session_id, stream=False):
            return result  # type: ignore[return-value]
        raise RuntimeError("No result returned from _run_with_session")

    async def stream_message(self, message: str, role: str | None = None, session_id: str | None = None) -> AsyncIterator[str]:
        async for chunk in self._run_with_session(message, role, session_id, stream=True):
            yield chunk  # type: ignore[misc]
