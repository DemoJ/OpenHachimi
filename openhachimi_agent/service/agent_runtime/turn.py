"""单轮对话编排。

从 `AgentService._run_with_session` 整体搬出。状态字典仍挂在 `AgentService`
实例上(`_session_states` / `_running_tasks` / `_session_locks` 等),
本模块通过传入的 service 引用读写,以保持与既有测试的兼容。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING

from pydantic_ai import ModelMessagesTypeAdapter

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.core.identifiers import validate_latest_scope
from openhachimi_agent.core.redaction import redact_exception, redact_text
from openhachimi_agent.memory.capture import capture_turn_memories
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.recall import recall_memories
from openhachimi_agent.service.agent_runtime.commands import SIGNAL_LABELS
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
from openhachimi_agent.storage.memory import load_message_history, save_message_history
from openhachimi_agent.transport.api_models import ArtifactRef, AttachmentRef, ChatResponse


if TYPE_CHECKING:
    from openhachimi_agent.service.agent_service import AgentService


logger = logging.getLogger(__name__)


def _error_message(exc: BaseException) -> str:
    return redact_exception(exc)


# WebUI 展示历史会话时需要"用户原始输入"，而 UserPromptPart 里实际只承载用户原话
# （v2 后已不再嵌 volatile 前缀），仍保留 metadata 旁路是为了：
# 1) 旧会话回放：旧版 UserPromptPart 里拼了 volatile 前缀，仅靠分隔符无法可靠反向
#    拆出原话，metadata 是稳妥的真值。
# 2) 兜底安全：万一未来又有路径往 user-prompt 塞了额外文本，metadata 仍能正确还原。
# 同时 stamp 一份"模型可见的完整 system 级上下文"快照,供 WebUI 在消息气泡的
# "展开运行时上下文"折叠区展示。
_USER_MESSAGE_METADATA_KEY = "openhachimi_user_message"
_SYSTEM_CONTEXT_METADATA_KEY = "openhachimi_system_context"


def _extract_system_prompt_text(messages: list) -> str:
    """从一组 ModelMessage 里聚合首个 ModelRequest 的所有 SystemPromptPart。

    pydantic-ai 把 ``Agent(system_prompt=...)``、``Agent(instructions=...)`` 以及
    所有 ``@agent.system_prompt`` 装饰器钩子返回的字符串都合并成一个或多个
    ``SystemPromptPart`` 放在首个 ``ModelRequest`` 里。我们按顺序拼接,就是
    模型在那一轮收到的完整系统消息文本(不含工具 schema —— 工具 schema 走
    OpenAI API 的 ``tools`` 字段,不在 message 里)。
    """
    from pydantic_ai.messages import ModelRequest, SystemPromptPart

    chunks: list[str] = []
    for msg in messages:
        if not isinstance(msg, ModelRequest):
            continue
        for part in getattr(msg, "parts", ()):
            if isinstance(part, SystemPromptPart):
                text = getattr(part, "content", "")
                if isinstance(text, str) and text.strip():
                    chunks.append(text)
        if chunks:
            break  # 只取首个含 SystemPromptPart 的 ModelRequest
    return "\n\n".join(chunks)


def _extract_tool_catalog(executor_agent: object) -> str:
    """提取 executor agent 当前可用的工具清单(工具名 + 一行描述)。

    模型实际通过 OpenAI API ``tools`` 字段收到完整 JSON schema,但展示在 WebUI 里
    没意义(太长且对用户不友好),所以只返回摘要清单。

    失败不抛异常,返回空串。
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
            # FunctionToolset.tools 是 dict[str, Tool]; 其它 toolset 可能是 list
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


def _build_full_system_context_snapshot(
    new_history: list,
    prev_len: int,
    executor_agent: object | None,
) -> str:
    """聚合本轮模型真正收到的所有 system 级上下文,用于 WebUI 展示。

    内容来源:
    - 首个 ModelRequest 的所有 SystemPromptPart(已含 base / executor / role
      instructions / runtime/config / runtime 动态块)
    - 工具清单摘要(name + 一行描述)

    其中第一部分占绝大多数,准确反映模型在本轮"接收到了什么"。
    """
    chunks: list[str] = []
    # 只看本轮新增的消息(prev_len 之后的 ModelRequest)
    relevant = new_history[prev_len:] if prev_len < len(new_history) else new_history
    system_text = _extract_system_prompt_text(relevant)
    if system_text:
        chunks.append(system_text)
    if executor_agent is not None:
        tool_summary = _extract_tool_catalog(executor_agent)
        if tool_summary:
            chunks.append(tool_summary)
    return "\n\n".join(chunks)


def _stamp_turn_metadata(
    new_history: list,
    prev_len: int,
    user_message: str,
    system_context: str,
) -> None:
    """给本轮新增的、首个含 ``UserPromptPart`` 的 ``ModelRequest`` 打两项 metadata：

    - ``openhachimi_user_message``：用户原始输入（不含任何系统注入）。
    - ``openhachimi_system_context``：本轮 system 级上下文完整快照(静态 system
      prompt + role instructions + 动态钩子 + 工具清单),给 WebUI 在"运行时
      上下文"折叠区展示。

    Multi-step 单轮中可能多次往 history 追加 ``ModelRequest``（planner、
    executor_repair 等都会 extend），但首个 user 消息就是本轮入口。
    """
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    if not user_message:
        return
    payload: dict[str, str] = {_USER_MESSAGE_METADATA_KEY: user_message}
    if system_context:
        payload[_SYSTEM_CONTEXT_METADATA_KEY] = system_context

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
        # metadata 已存在：补齐我们这两项,不覆盖第三方已有键
        for k, v in payload.items():
            if k not in meta:
                meta[k] = v
        return


# 旧名保留为别名,避免第三方/旧代码导入断裂
_stamp_user_message_metadata = lambda new_history, prev_len, user_message: _stamp_turn_metadata(  # noqa: E731
    new_history, prev_len, user_message, ""
)


async def run_turn(
    service: "AgentService",
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
    """运行单轮对话,产出流式事件或最终 ChatResponse。

    与原 `AgentService._run_with_session` 行为一致;状态仍由 `service` 持有。
    """
    start_time = time.perf_counter()
    role = service._normalize_role(role)
    session_id = service._normalize_session_id(session_id)
    service._validate_role_exists(role)
    latest_scope = (
        validate_latest_scope(str(channel_context.get("session_scope_key")))
        if channel_context and channel_context.get("session_scope_key")
        else None
    )
    attachment_list = list(attachments or [])
    effective_message = message_with_attachments(message, attachment_list)

    channel_context_data = dict(channel_context or {})
    if not channel_context_data:
        channel_context_data = {"type": channel or "local", "platform": channel or "local"}
        if delivery_target:
            channel_context_data.update(delivery_target)
    channel_name = str(channel_context_data.get("type") or channel_context_data.get("platform") or "local")

    actual_session_id, history = load_message_history(service.config.memory_dir, role, session_id, latest_scope)
    lock = service._get_session_lock(actual_session_id)

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
        await service._maybe_reload_mcp_toolsets()

        if actual_session_id not in service._session_states:
            service._session_states[actual_session_id] = {}
        session_state = service._session_states[actual_session_id]
        session_state["turn_artifacts"] = []
        memory_scope = MemoryScope(
            tenant_id="local",
            user_id="local",
            role_name=role,
            session_id=actual_session_id,
            channel=channel_name,
        )
        memory_context = recall_memories(service.config, memory_scope, effective_message)
        session_state["memory_context"] = memory_context
        deps = AgentDeps(
            config=service.config,
            session_id=actual_session_id,
            browser_manager=service.browser_manager,
            process_manager=service.process_manager,
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
            config=service.config,
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
        ctx.context_compressor = service._get_context_compressor(actual_session_id, memory_scope)

        async def refresh_mcp_config() -> None:
            await service._maybe_reload_mcp_toolsets()
            ctx.config = service.config
            deps.config = service.config

        should_route = await should_route_message(ctx, service._get_agent)

        async def run_agent() -> None:
            mark_turn_started(session_state)
            try:
                if should_route:
                    await refresh_mcp_config()
                    task_frame = await resolve_task_frame(ctx, service._get_agent)
                    session_state["task_frame"] = task_frame.model_dump(mode="json")
                    if needs_planning(task_frame):
                        await refresh_mcp_config()
                        await run_planner(ctx, task_frame, service._get_agent)

                await refresh_mcp_config()
                outcome = await execute_task(ctx, service._get_agent)
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
                        "Agent 执行超时:"
                        f"{service.config.agent_timeout_seconds}s 内没有完成。"
                        f"模型={service.config.model_name},"
                        f"base_url={redact_text(service.config.openai_base_url or '默认')},"
                        f"role={role},session_id={actual_session_id}。"
                        "常见原因:模型服务无响应、工具调用卡住、浏览器/网络代理不可用。"
                    )
                    logger.exception(
                        "chat timed out role=%s session_id=%s timeout_seconds=%d stream=true",
                        role,
                        actual_session_id,
                        service.config.agent_timeout_seconds,
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
        service._running_tasks[actual_session_id] = task

        try:
            if stream:
                try:
                    async for event in consume_stream_queue(
                        stream_queue=stream_queue,
                        task=task,
                        config=service.config,
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
                            "\n\n[System] 当前任务已暂停:"
                            f"{exc} 旧计划已挂起,不会影响下一轮对话;"
                            "如需恢复,请明确说明\"继续刚才的任务\"。"
                        )
                    else:
                        fail_current_plan(session_state, reason="operation_stalled", detail=stalled_detail)
                        yield system_stream_event(f"\n\n[System] 当前任务已失败:{exc} 未生成可恢复计划,下一轮将重新理解用户请求。")
                    return

                try:
                    await task
                except asyncio.CancelledError:
                    if task.cancelled():
                        yield system_stream_event("\n\n【任务已被手动中断】")
                        return
                    raise

                if error := result_holder.get("error"):
                    raise RuntimeError(f"Agent 调用失败:{_error_message(error)}") from error
                for signal_key, signal_label in SIGNAL_LABELS:
                    if signal_value := result_holder.get(signal_key):
                        yield system_stream_event(
                            f"\n\n{signal_label}{json.dumps(signal_value, ensure_ascii=False)}"
                        )
                turn_artifacts = [
                    artifact for artifact in session_state.get("turn_artifacts", [])
                    if isinstance(artifact, ArtifactRef)
                ]
                service.register_artifacts(turn_artifacts)
                seen_artifacts: set[str] = set()
                for artifact in turn_artifacts:
                    if artifact.id in seen_artifacts:
                        continue
                    seen_artifacts.add(artifact.id)
                    yield StreamEventItem(
                        type="artifact",
                        text=f"已生成文件:{artifact.filename}",
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
            service.register_artifacts(turn_artifacts)
            new_history = list(result.all_messages())  # type: ignore[attr-defined]

            # 把"用户原始输入"持久化到本轮 ModelRequest 的 metadata 里。
            # 这样 WebUI 等下游展示历史会话时可以精确取到用户那句话，无需启发式
            # 拆分被注入的 volatile 前缀（时间/记忆/技能等）。
            # 找的是本轮新增（idx >= len(history)）且含 UserPromptPart 的第一个 ModelRequest，
            # 通常就是承载本轮用户消息的那条。
            # 把"用户原始输入"和"系统级上下文完整快照"持久化到本轮 ModelRequest metadata。
            # WebUI 用 system_context 展示"运行时上下文"折叠区——其内容是模型在本轮
            # 真正收到的全部 system 级文本(base prompt + executor role + role
            # instructions + runtime/config + 动态钩子 + 工具清单),不只是动态钩子部分。
            try:
                # 在持久化阶段拿到一份"当前角色 executor agent",用于工具清单提取。
                # 注意:这里不再产生新的 LLM 调用,仅用来 introspect toolset。
                _executor_for_intro = service._get_agent(role, "executor")
            except Exception:
                _executor_for_intro = None
            try:
                _system_context = _build_full_system_context_snapshot(
                    new_history, len(history), _executor_for_intro
                )
            except Exception:
                logger.debug("system context snapshot failed", exc_info=True)
                _system_context = ""
            _stamp_turn_metadata(new_history, len(history), message, _system_context)
            # 上下文压缩:用本轮真实用量判定,触发则压缩(含 LLM 摘要,经 to_thread 避免阻塞事件循环)
            compressor = ctx.context_compressor
            if compressor is not None:
                try:
                    compressor.update_from_response(result.usage)  # type: ignore[attr-defined]
                except Exception:
                    logger.debug("context usage update failed", exc_info=True)
                if compressor.should_compress():
                    try:
                        new_history = await asyncio.to_thread(
                            compressor.compress,
                            new_history,
                            current_tokens=compressor.last_prompt_tokens,
                        )
                        logger.info(
                            "context compressed post-turn role=%s session_id=%s messages=%d compression_count=%d",
                            role,
                            actual_session_id,
                            len(new_history),
                            compressor.compression_count,
                        )
                    except Exception:
                        logger.warning(
                            "context compression failed role=%s session_id=%s",
                            role,
                            actual_session_id,
                            exc_info=True,
                        )
            history_json = ModelMessagesTypeAdapter.dump_json(new_history)

            await asyncio.to_thread(
                save_message_history,
                service.config.memory_dir,
                role,
                actual_session_id,
                history_json,
                latest_scope,
            )
            capture_args = (
                service.config,
                memory_scope,
                effective_message,
                str(result.output),  # type: ignore[attr-defined]
            )
            # turn 来源:scheduled / system 不进 L1 抽取(由 capture_turn_memories 处理)
            if run_mode == "scheduled":
                capture_source = "scheduled"
            elif run_mode == "system":
                capture_source = "system"
            else:
                capture_source = "user"
            capture_kwargs = {
                "task_frame": session_state.get("task_frame") if isinstance(session_state.get("task_frame"), dict) else None,
                "memory_context_ids": memory_context.ids,
                "duration_ms": int((time.perf_counter() - start_time) * 1000),
                "source": capture_source,
            }
            if service.config.memory.capture.async_enabled:
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
            service._running_tasks.pop(actual_session_id, None)
            if not task.done():
                task.cancel()
