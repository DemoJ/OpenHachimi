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


from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.core.identifiers import validate_latest_scope
from openhachimi_agent.core.redaction import redact_exception, redact_text
from openhachimi_agent.memory.capture import capture_turn_memories
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.recall import recall_memories
from openhachimi_agent.service.agent_runtime.commands import (
    SIGNAL_LABELS,
    channel_code_from_context,
)
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


# 旧名保留为别名,避免第三方/旧代码导入断裂
_stamp_user_message_metadata = lambda new_history, prev_len, user_message: _stamp_turn_metadata(  # noqa: E731
    new_history, prev_len, user_message, "", ""
)


def _build_executor_static_context(
    config: AppConfig,
    role: str,
    executor_agent: object,
    service: "AgentService | None" = None,
) -> str:
    """直接构建 executor 的静态 system prompt 段(不含每轮动态注入的部分)。

    包括:base.md + agents/executor.md + role instructions + runtime/config.md
    + 可用工具摘要清单。
    """
    from openhachimi_agent.content.prompts import load_system_prompt, render_system_prompt
    from openhachimi_agent.content.roles import load_role_content

    chunks: list[str] = []
    try:
        base = load_system_prompt("base")
        if base:
            chunks.append(base)
    except Exception:
        logger.debug("failed to load base.md", exc_info=True)
    try:
        executor_prompt = load_system_prompt("agents/executor")
        if executor_prompt:
            chunks.append(executor_prompt)
    except Exception:
        logger.debug("failed to load executor.md", exc_info=True)
    try:
        role_content = load_role_content(config.roles_dir, role)
        if role_content:
            chunks.append(role_content)
    except Exception:
        logger.debug("failed to load role content role=%s", role, exc_info=True)
    try:
        config_prompt = render_system_prompt("runtime/config", {"user_dir": str(config.user_dir).replace("\\", "/")})
        if config_prompt:
            chunks.append(config_prompt)
    except Exception:
        logger.debug("failed to render config.md", exc_info=True)

    # 工具目录摘要 — 优先走 service 缓存
    if service is not None:
        try:
            tool_text = service._get_cached_tool_catalog(role, executor_agent)
            if tool_text:
                chunks.append(tool_text)
        except Exception:
            logger.debug("failed to get tool catalog from service", exc_info=True)
    else:
        try:
            tool_text = _extract_tool_catalog(executor_agent)
            if tool_text:
                chunks.append(tool_text)
        except Exception:
            logger.debug("failed to extract tool catalog", exc_info=True)

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
    # 渠道归属：从 channel_context 提取 channel_code（仅接受 CHANNEL_CODES 内的值），
    # 未识别时退到外层 channel 形参。后续传给 save_message_history 写 sidecar。
    resolved_channel_code = channel_code_from_context(channel_context)
    attachment_list = list(attachments or [])
    effective_message = message_with_attachments(message, attachment_list)

    channel_context_data = dict(channel_context or {})
    if not channel_context_data:
        channel_context_data = {"type": channel or "local", "platform": channel or "local"}
        if delivery_target:
            channel_context_data.update(delivery_target)
    channel_name = str(channel_context_data.get("type") or channel_context_data.get("platform") or "local")

    actual_session_id, history = service.session_store.load_messages(role, session_id, latest_scope)
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
            session_store=service.session_store,
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

        # MCP 配置在单轮内只刷新一次:router/planner/executor 三段编排原本各自调用一次,
        # 实际同一轮内文件 mtime 不会变,重复 stat + signature compare 是浪费。
        # closure 标志位负责短路第 2/3 次调用。
        _mcp_refreshed = False

        async def refresh_mcp_config() -> None:
            nonlocal _mcp_refreshed
            if _mcp_refreshed:
                return
            _mcp_refreshed = True
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

            # 持久化阶段构造 executor 系统级上下文快照,分两段写入 ModelRequest metadata:
            # - openhachimi_ctx_dynamic:本轮变的(时间/TaskFrame/记忆/技能)
            # - openhachimi_ctx_static_hash:稳定段(base/executor/role/config/tools)的短哈希
            # 完整静态文本写到 service._context_static_pool,读取时再回填。
            # 注意:这里不再产生新的 LLM 调用,executor agent 仅用来 introspect toolset。
            _static_text = ""
            _dynamic_text = ""
            _static_hash = ""
            try:
                _executor_for_intro = service._get_agent(role, "executor")
            except Exception:
                _executor_for_intro = None
            try:
                _static_text, _dynamic_text, _static_hash = _snapshot_executor_context(
                    service.config, role, _executor_for_intro, deps, service=service
                )
            except Exception:
                logger.debug("system context snapshot failed", exc_info=True)
            if _static_hash and _static_text:
                try:
                    service._ensure_context_static(_static_hash, _static_text)
                except Exception:
                    logger.debug("failed to register static context to pool", exc_info=True)
            _stamp_turn_metadata(new_history, len(history), message, _dynamic_text, _static_hash)
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
            # SessionStore.save_messages 接 list[ModelMessage],store 内部按条入 SQLite。
            # 这里不再手动 dump_json —— v3 文件方案的"每轮全量字节落盘"已经退役。
            # scope_key 与旧 save_message_history 一致沿用 latest_scope 值。
            await asyncio.to_thread(
                service.session_store.save_messages,
                role,
                actual_session_id,
                new_history,
                scope=latest_scope,
                channel=resolved_channel_code,
                scope_key=latest_scope,
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
