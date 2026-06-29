"""localhost HTTP daemon。

通过 FastAPI lifespan 机制，在服务启动时自动启动 Telegram Bot（若已配置 token），
服务关闭时优雅停止 Bot。所有渠道共享同一 asyncio 事件循环。
"""

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import (
    SETTINGS_FIELD_GROUPS,
    apply_config_updates,
    get_mcp_config,
    load_config,
    load_raw_config,
    load_roles_config,
    serialize_config_group,
    write_mcp_config,
    write_roles_config,
)
from openhachimi_agent.core.config.models import MCPServerConfig
from openhachimi_agent.core.identifiers import safe_role_file_path, validate_role_name
from openhachimi_agent.core.redaction import safe_error_detail
from openhachimi_agent.content.roles import list_role_names
from openhachimi_agent.content.skills import find_skills, set_skill_disable_model_invocation
from openhachimi_agent.tools.skills import install_skill_from_source, delete_skill_dir
from openhachimi_agent.interface.telegram import telegram_lifespan
from openhachimi_agent.interface.weixin.channel import weixin_lifespan
from openhachimi_agent.scheduler.delivery import (
    CliDeliverySender,
    DeliverySenderRegistry,
    InboxDeliverySender,
    TelegramDeliverySender,
    deliver_scheduled_run,
)
from openhachimi_agent.scheduler.service import ScheduledTaskService, run_to_dict, task_to_dict
from openhachimi_agent.scheduler.store import ScheduledTaskStore
from openhachimi_agent.scheduler.scheduler import TaskScheduler
from openhachimi_agent.scheduler.models import ScheduledRun, ScheduledTask
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.transport.api_models import (
    ChannelListResponse,
    ChatRequest,
    CommandDispatchRequest,
    CommandDispatchResponse,
    CommandResponse,
    ConfigUpdateRequest,
    DeliveryPreviewResponse,
    MemoryDeleteRequest,
    MemoryDeleteResult,
    MemoryItem,
    MemoryListResponse,
    MemoryUpdateRequest,
    MemoryUpdateResult,
    MessageItem,
    MCPServerItem,
    McpServersResponse,
    McpServersUpdateRequest,
    PromptUpdateRequest,
    RoleSwitchRequest,
    RoleBindingItem,
    RoleOption,
    RolesConfigResponse,
    RolesConfigUpdateRequest,
    ScheduleCreateRequest,
    ScheduleDeliveryUpdateRequest,
    ScheduleResponse,
    ScheduleRunResponse,
    ScheduleUpdateRequest,
    SessionListResponse,
    SessionLoadRequest,
    SessionMessagesResponse,
    SessionSummary,
    SkillDeleteRequest,
    SkillDeleteResult,
    SkillInstallRequest,
    SkillInstallResult,
    SkillItem,
    SkillsResponse,
    SkillToggleRequest,
    SkillToggleResult,
    StopRequest,
)
from openhachimi_agent.tools.utils import resolve_workspace_path
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.privacy import PrivacyGuard
from openhachimi_agent.memory.recall import get_memory_store
from openhachimi_agent.memory.scheduler import MemoryScheduler

logger = logging.getLogger(__name__)


def get_service(request: Request) -> AgentService:
    return request.app.state.service


def get_config(request: Request):
    return request.app.state.config


def get_schedule_store(request: Request) -> ScheduledTaskStore:
    store = getattr(request.app.state, "schedule_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="定时任务未启用")
    return store


def get_schedule_service(request: Request) -> ScheduledTaskService:
    store = get_schedule_store(request)
    return ScheduledTaskService(store)


def get_scheduler(request: Request) -> TaskScheduler:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="定时任务调度器未启动")
    return scheduler


def get_delivery_registry(request: Request) -> DeliverySenderRegistry:
    registry = getattr(request.app.state, "delivery_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="投递系统未初始化")
    return registry


def _dt_text(value) -> str | None:
    return value.isoformat() if value else None


def schedule_response(task: ScheduledTask) -> ScheduleResponse:
    return ScheduleResponse(
        id=task.id,
        name=task.name,
        prompt=task.prompt,
        schedule_type=task.schedule_type.value,
        schedule_expr=task.schedule_expr,
        timezone=task.timezone,
        status=task.status,
        enabled=task.enabled,
        role=task.role,
        session_id=task.session_id,
        timeout_seconds=task.timeout_seconds,
        origin=task.origin,
        delivery_mode=task.delivery_mode,
        delivery_targets=task.delivery_targets,
        delivery_fallback=task.delivery_fallback,
        execution_policy=task.execution_policy,
        safety_status=task.safety_status,
        safety_error=task.safety_error,
        next_run_at=_dt_text(task.next_run_at),
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        last_run_at=_dt_text(task.last_run_at),
        last_status=task.last_status,
        last_error=task.last_error,
        last_delivery_status=task.last_delivery_status,
        last_delivery_error=task.last_delivery_error,
        running=task.running,
    )


def schedule_run_response(run: ScheduledRun) -> ScheduleRunResponse:
    return ScheduleRunResponse(
        id=run.id,
        task_id=run.task_id,
        status=run.status,
        started_at=run.started_at.isoformat(),
        finished_at=_dt_text(run.finished_at),
        output=run.output,
        error=run.error,
        duration_ms=run.duration_ms,
        delivery_status=run.delivery_status,
        delivery_targets=run.delivery_targets,
        delivery_results=run.delivery_results,
        delivery_error=run.delivery_error,
        delivered_at=_dt_text(run.delivered_at),
        read_at=_dt_text(run.read_at),
        safety_status=run.safety_status,
        safety_error=run.safety_error,
        execution_context=run.execution_context,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 应用生命周期管理器。"""
    config = load_config()
    configure_logging(config)
    app.state.service = AgentService(config)
    await app.state.service.start()
    app.state.config = config
    app.state.schedule_store = None
    app.state.scheduler = None
    app.state.memory_scheduler = None
    app.state.delivery_registry = DeliverySenderRegistry()
    app.state.delivery_registry.register(InboxDeliverySender())
    scheduler = None
    try:
        async with telegram_lifespan(config, app.state.service) as telegram_sender:
            app.state.telegram_sender = telegram_sender
            if telegram_sender is not None:
                app.state.delivery_registry.register(TelegramDeliverySender(telegram_sender))

            async with weixin_lifespan(app):
                if config.scheduler.enabled and config.scheduler.db_path is not None:
                    app.state.schedule_store = ScheduledTaskStore(config.scheduler.db_path)

                async def on_scheduled_run_complete(task: ScheduledTask, run: ScheduledRun) -> None:
                    await deliver_scheduled_run(
                        task,
                        run,
                        store=app.state.schedule_store,
                        registry=app.state.delivery_registry,
                        config=config,
                    )

                scheduler = TaskScheduler(
                    app.state.schedule_store,
                    app.state.service,
                    poll_interval_seconds=config.scheduler.poll_interval_seconds,
                    max_concurrency=config.scheduler.max_concurrency,
                    default_timeout_seconds=config.scheduler.default_timeout_seconds,
                    claim_lock_seconds=config.scheduler.claim_lock_seconds,
                    delivery_registry=app.state.delivery_registry,
                    config=config,
                    on_run_complete=on_scheduled_run_complete,
                )
                app.state.scheduler = scheduler
                await scheduler.start()
                # 长期记忆后台调度器:消费 memory_jobs 队列(embed/extract/consolidate/maintenance)。
                # 不启动则队列无人消费,记忆永远停在 pending(embeddings_pending 虚高、L1 抽取不跑)。
                memory_scheduler: MemoryScheduler | None = None
                if config.memory.enabled and config.memory.scheduler.enabled:
                    memory_scheduler = MemoryScheduler(
                        get_memory_store(config),
                        config=config,
                        poll_interval_seconds=config.memory.scheduler.poll_interval_seconds,
                        batch_size=config.memory.scheduler.batch_size,
                    )
                    app.state.memory_scheduler = memory_scheduler
                    await memory_scheduler.start()
                    logger.info("memory scheduler started")
                logger.info("server module initialized")
                logger.info("all channels started")
                yield
                logger.info("all channels stopping")
    finally:
        if scheduler is not None:
            await scheduler.stop()
        try:
            if app.state.memory_scheduler is not None:
                await app.state.memory_scheduler.stop()
        except Exception as exc:
            logger.debug("memory scheduler stop failed: %s", exc)
        try:
            await app.state.service.browser_manager.close()
        except Exception as exc:
            logger.debug("browser cleanup on server shutdown failed: %s", exc)
        try:
            await app.state.service.stop()
        except Exception as exc:
            logger.debug("service stop failed: %s", exc)


app = FastAPI(title="OpenHachimi Agent", lifespan=lifespan)


@app.middleware("http")
async def require_http_api_token(request: Request, call_next):
    if request.url.path == "/health" or request.url.path.startswith("/ui"):
        return await call_next(request)

    config = getattr(request.app.state, "config", None)
    token = getattr(config, "http_api_token", None)
    if not token:
        return JSONResponse(status_code=503, content={"detail": "HTTP API Token 未初始化"})

    auth = request.headers.get("authorization", "")
    expected = f"Bearer {token}"
    if not hmac.compare_digest(auth, expected):
        return JSONResponse(status_code=401, content={"detail": "未授权"})
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    from openhachimi_agent.core.version import get_version

    logger.debug("health check")
    return {"status": "ok", "version": get_version()}


@app.get("/state")
def state(service: AgentService = Depends(get_service)):
    return service.state()


@app.get("/roles")
def roles(service: AgentService = Depends(get_service)):
    return service.list_roles()


@app.get("/schedules")
def list_schedules(
    include_deleted: bool = False,
    svc: ScheduledTaskService = Depends(get_schedule_service),
) -> list[ScheduleResponse]:
    return [schedule_response(task) for task in svc.list(include_deleted=include_deleted)]


@app.post("/schedules")
def create_schedule(
    request: ScheduleCreateRequest,
    svc: ScheduledTaskService = Depends(get_schedule_service),
) -> ScheduleResponse:
    origin = dict(request.origin or {})
    origin.setdefault("type", "http")
    origin.setdefault("platform", "http")
    try:
        task = svc.create(
            name=request.name,
            prompt=request.prompt,
            schedule_type=request.schedule_type,
            schedule_expr=request.schedule_expr,
            timezone=request.timezone,
            role=request.role,
            session_id=request.session_id,
            timeout_seconds=request.timeout_seconds,
            origin=origin,
            delivery_mode=request.delivery_mode,
            delivery_targets=request.delivery_targets,
            delivery_fallback=request.delivery_fallback,
            execution_policy=request.execution_policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc
    return schedule_response(task)


@app.get("/schedules/inbox")
def list_schedule_inbox(
    unread_only: bool = True,
    limit: int = 20,
    mark_read: bool = False,
    svc: ScheduledTaskService = Depends(get_schedule_service),
) -> list[ScheduleRunResponse]:
    items = svc.read_inbox(unread_only=unread_only, limit=limit, mark_read=mark_read)
    return [schedule_run_response(run) for _task, run in items]


@app.post("/schedules/inbox/{run_id}/read")
def mark_schedule_run_read(run_id: str, svc: ScheduledTaskService = Depends(get_schedule_service)) -> dict[str, bool]:
    try:
        svc.mark_read(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运行记录不存在") from exc
    return {"ok": True}


@app.get("/schedules/{task_id}")
def get_schedule(task_id: str, svc: ScheduledTaskService = Depends(get_schedule_service)) -> ScheduleResponse:
    try:
        task = svc.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    return schedule_response(task)


@app.patch("/schedules/{task_id}")
def update_schedule(task_id: str, request: ScheduleUpdateRequest, svc: ScheduledTaskService = Depends(get_schedule_service)) -> ScheduleResponse:
    updates = request.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="没有提供可更新字段")
    try:
        task = svc.update(task_id, **updates)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc
    return schedule_response(task)


@app.patch("/schedules/{task_id}/delivery")
def update_schedule_delivery(
    task_id: str,
    request: ScheduleDeliveryUpdateRequest,
    svc: ScheduledTaskService = Depends(get_schedule_service),
) -> ScheduleResponse:
    try:
        task = svc.update_delivery(
            task_id,
            delivery_mode=request.delivery_mode,
            delivery_targets=request.delivery_targets,
            delivery_fallback=request.delivery_fallback,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    return schedule_response(task)


@app.delete("/schedules/{task_id}")
def delete_schedule(task_id: str, svc: ScheduledTaskService = Depends(get_schedule_service)) -> dict[str, bool]:
    try:
        svc.remove(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    return {"ok": True}


@app.post("/schedules/{task_id}/pause")
def pause_schedule(task_id: str, svc: ScheduledTaskService = Depends(get_schedule_service)) -> ScheduleResponse:
    try:
        task = svc.pause(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    return schedule_response(task)


@app.post("/schedules/{task_id}/resume")
def resume_schedule(task_id: str, svc: ScheduledTaskService = Depends(get_schedule_service)) -> ScheduleResponse:
    try:
        task = svc.resume(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    return schedule_response(task)


@app.post("/schedules/{task_id}/run")
async def run_schedule_now(
    task_id: str,
    scheduler: TaskScheduler = Depends(get_scheduler),
    store: ScheduledTaskStore = Depends(get_schedule_store),
) -> ScheduleRunResponse:
    try:
        task = await asyncio.to_thread(store.claim_task_now, task_id, scheduler.claim_lock_seconds)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=safe_error_detail(exc)) from exc
    run = await scheduler.run_task_now(task, preserve_schedule=True)
    if run is None:
        raise HTTPException(status_code=500, detail="任务执行失败")
    return schedule_run_response(run)


@app.get("/schedules/{task_id}/runs")
def list_schedule_runs(task_id: str, limit: int = 20, svc: ScheduledTaskService = Depends(get_schedule_service)) -> list[ScheduleRunResponse]:
    try:
        runs = svc.list_runs(task_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    return [schedule_run_response(run) for run in runs]


@app.get("/schedules/{task_id}/runs/{run_id}")
def get_schedule_run(run_id: str, svc: ScheduledTaskService = Depends(get_schedule_service)) -> ScheduleRunResponse:
    try:
        run = svc.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运行记录不存在") from exc
    return schedule_run_response(run)


@app.post("/schedules/{task_id}/delivery/preview")
def preview_schedule_delivery(task_id: str, svc: ScheduledTaskService = Depends(get_schedule_service)) -> DeliveryPreviewResponse:
    try:
        preview = svc.preview_delivery(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    return DeliveryPreviewResponse(**preview)


@app.post("/chat")
async def chat(request: ChatRequest, service: AgentService = Depends(get_service)):
    logger.info("http chat request message_chars=%d attachment_count=%d stream=false", len(request.message), len(request.attachments))
    channel_code = request.channel or "webui"
    # WebUI/外部 API 调用 /chat 时,若没指定 session_id,scope_key 用 channel_code 自身
    # —— 让每个渠道有独立的 latest 指针,不再跟其他渠道共用全局 latest。
    session_scope_key = channel_code if request.session_id is None else None
    resolved_session_id = request.session_id
    if resolved_session_id is None and channel_code == "webui":
        # WebUI 空白页直发:自动新建一条 session 并绑死渠道,等价于自动触发一次 /new。
        new_resp = service.new_session_for_channel(request.role, channel_code)
        resolved_session_id = new_resp.session_id
    channel_context = {
        "type": "http",
        "platform": "http",
        "channel_code": channel_code,
        "session_id": resolved_session_id,
        "role": request.role,
    }
    if session_scope_key:
        channel_context["session_scope_key"] = session_scope_key
    try:
        return await service.send_message(
            request.message,
            request.role,
            resolved_session_id,
            attachments=request.attachments,
            channel_context=channel_context,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=safe_error_detail(exc)) from exc


@app.post("/chat/stream")
def chat_stream(
    api_request: ChatRequest,
    http_request: Request,
    service: AgentService = Depends(get_service),
):
    logger.info(
        "http chat request message_chars=%d attachment_count=%d stream=true",
        len(api_request.message),
        len(api_request.attachments),
    )
    channel_code = api_request.channel or "webui"
    resolved_session_id = api_request.session_id
    auto_created = False
    if resolved_session_id is None and channel_code == "webui":
        # 空白页直发 → 自动新建会话,sidecar 立刻把它绑到 webui 渠道。
        new_resp = service.new_session_for_channel(api_request.role, channel_code)
        resolved_session_id = new_resp.session_id
        auto_created = True
    # 没指定 session 时,scope_key 用 channel_code,保证后续 latest 走 latest_by_scope/
    session_scope_key = channel_code if api_request.session_id is None else None

    async def sse_generator():
        event_count = 0
        channel_context = {
            "type": "http",
            "platform": "http",
            "channel_code": channel_code,
            "session_id": resolved_session_id,
            "role": api_request.role,
        }
        if session_scope_key:
            channel_context["session_scope_key"] = session_scope_key
        logger.info(
            "sse stream opened role=%s session_id=%s channel=%s auto_created=%s message_chars=%d",
            api_request.role,
            resolved_session_id,
            channel_code,
            auto_created,
            len(api_request.message),
        )
        try:
            # 把后端解析出的 session_id 通过首事件回传给前端,让 store.currentSessionId
            # 在空白页直发场景下能立刻同步——避免 SSE 流结束后还要靠 sessions[0] 兜底。
            if resolved_session_id:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "session",
                            "session_id": resolved_session_id,
                            "channel": channel_code,
                            "auto_created": auto_created,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                event_count += 1

            async for event in service.stream_events(
                api_request.message,
                api_request.role,
                resolved_session_id,
                attachments=api_request.attachments,
                channel_context=channel_context,
            ):
                # 每发出一个事件前检查客户端是否已断开，
                # 避免后端在无人消费的 stream 上继续运行。
                if await http_request.is_disconnected():
                    logger.warning(
                        "sse stream closing reason=client_disconnected "
                        "event_count=%d role=%s session_id=%s",
                        event_count,
                        api_request.role,
                        resolved_session_id,
                    )
                    return

                payload = {
                    "type": event.type,
                    "text": event.text,
                    "temporary": event.temporary,
                }
                if event.tool_name:
                    payload["tool_name"] = event.tool_name
                if event.tool_icon:
                    payload["tool_icon"] = event.tool_icon
                if event.artifact:
                    payload["artifact"] = event.artifact.model_dump(mode="json")
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                event_count += 1

            yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"
            logger.info(
                "sse stream closing reason=done event_count=%d role=%s session_id=%s",
                event_count,
                api_request.role,
                resolved_session_id,
            )
        except Exception as exc:
            logger.exception(
                "sse stream closing reason=error event_count=%d role=%s session_id=%s",
                event_count,
                api_request.role,
                resolved_session_id,
            )
            yield f"data: {json.dumps({'error': safe_error_detail(exc)}, ensure_ascii=False)}\n\n"

    try:
        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=safe_error_detail(exc)) from exc


@app.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, service: AgentService = Depends(get_service)):
    artifact = service.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact 不存在")
    try:
        target = resolve_workspace_path(service.config.base_dir, artifact.local_path)
    except Exception as exc:
        raise HTTPException(status_code=403, detail="artifact 路径不合法") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="artifact 文件不存在")
    size = target.stat().st_size
    if size > service.config.max_attachment_size_bytes:
        raise HTTPException(status_code=413, detail="artifact 文件超过大小上限")
    return FileResponse(
        target,
        media_type=artifact.content_type or "application/octet-stream",
        filename=artifact.filename or Path(target).name,
    )


@app.post("/new")
def new_session(role: str | None = None, service: AgentService = Depends(get_service)):
    logger.info("http new session request role=%s", role)
    return service.new_session(role)


@app.get("/session/latest")
def latest_session(role: str | None = None, service: AgentService = Depends(get_service)):
    logger.info("http latest session request role=%s", role)
    return service.latest_session(role)


@app.get("/sessions")
def list_sessions(
    role: str | None = None,
    channel: str | None = None,
    limit: int = Query(50, ge=1, le=200, description="单页最大返回会话数"),
    offset: int = Query(0, ge=0, description="跳过条数,offset-based 分页"),
    service: AgentService = Depends(get_service),
) -> SessionListResponse:
    logger.info(
        "http list sessions request role=%s channel=%s limit=%d offset=%d",
        role, channel, limit, offset,
    )
    return SessionListResponse(
        **service.list_sessions(role, channel=channel, limit=limit, offset=offset)
    )


@app.get("/channels")
def list_channels() -> ChannelListResponse:
    """返回可选渠道枚举,前端筛选下拉用。"""
    from openhachimi_agent.storage.session_store import CHANNEL_CODES, DEFAULT_CHANNEL

    return ChannelListResponse(channels=list(CHANNEL_CODES), default=DEFAULT_CHANNEL)


@app.post("/sessions/load")
def load_session(request: SessionLoadRequest, service: AgentService = Depends(get_service)):
    logger.info("http load session request session_id=%s role=%s", request.session_id, request.role)
    try:
        return service.load_session(request.role, request.session_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc


@app.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str, role: str | None = None, service: AgentService = Depends(get_service)) -> SessionMessagesResponse:
    logger.info("http get session messages request session_id=%s role=%s", session_id, role)
    try:
        return SessionMessagesResponse(**service.get_session_messages(role, session_id))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, role: str | None = None, service: AgentService = Depends(get_service)) -> CommandResponse:
    """删除指定会话:消息历史、TODO、最新指针一并清除,不可撤销。"""
    logger.info("http delete session request session_id=%s role=%s", session_id, role)
    try:
        return service.delete_session(role, session_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc


@app.post("/role")
def switch_role(request: RoleSwitchRequest, service: AgentService = Depends(get_service)):
    logger.info("http switch role request role=%s", request.role.strip())
    try:
        return service.switch_role(request.role.strip())
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc


# ------------------------------------------------------------------ WebUI 设置页配置
# GET /config/{group} 读取一个设置分组;PATCH /config/{group} 写回(保留注释)。
# 直接读写 config.yaml 原始内容,不走 AppConfig,避免单位转换破坏文件语义。
# secret 字段返回时掩码,提交时若与掩码相同视为未改动(跳过),空串表示清除。


@app.get("/config/{group}")
def get_config_group(group: str, config=Depends(get_config)):
    fields = SETTINGS_FIELD_GROUPS.get(group)
    if fields is None:
        raise HTTPException(status_code=404, detail=f"未知的设置分组: {group}")
    raw = load_raw_config(config.config_path)
    values, masked = serialize_config_group(fields, raw)
    # 同时返回字段定义,前端据此渲染控件(无需在前端硬编码字段表)。
    return {
        "group": group,
        "fields": fields,
        "values": values,
        "masked": masked,
    }


@app.patch("/config/{group}")
def update_config_group(group: str, request: ConfigUpdateRequest, config=Depends(get_config)):
    fields = SETTINGS_FIELD_GROUPS.get(group)
    if fields is None:
        raise HTTPException(status_code=404, detail=f"未知的设置分组: {group}")
    updates = request.updates
    if not isinstance(updates, dict):
        raise HTTPException(status_code=400, detail="updates 必须是对象")

    # 重新读最新原始配置,避免与其他写回并发产生覆盖。
    raw = load_raw_config(config.config_path)
    try:
        result = apply_config_updates(config.config_path, raw, fields, updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc

    logger.info("http config update group=%s written=%s skipped=%s", group, result["written"], result["skipped"])
    # 返回最新值(已脱敏),供前端刷新表单 + dirty 复位。
    values, masked = serialize_config_group(fields, raw)
    return {"group": group, "values": values, "masked": masked, **result}


# ------------------------------------------------------------------ WebUI 提示词编辑
# GET /prompts 返回首批可编辑提示词元数据 + 当前生效内容 + 是否已被用户覆盖;
# PATCH /prompts 写回某个提示词:非空写 user/system_prompts/<name>.md,清空删该文件(回退内置)。
# 与 /config 不同:提示词数据形态为整文件多行文本,不走 yaml 字段表与写回引擎。

from openhachimi_agent.content.prompt_registry import PROMPTS as _PROMPT_SPECS
from openhachimi_agent.content.prompts import (
    delete_override,
    is_overridden,
    load_system_prompt,
    write_override,
)


def _prompt_spec_to_dict(spec):
    return {
        "name": spec.name,
        "title": spec.title,
        "description": spec.description,
        "has_template_vars": spec.has_template_vars,
        "restart_note": spec.restart_note,
        # content = 当前生效值(覆盖优先,回退内置),前端 textarea 直接显示所见即所得。
        "content": load_system_prompt(spec.name),
        "is_overridden": is_overridden(spec.name),
    }


@app.get("/prompts")
def get_prompts(config=Depends(get_config)):
    return {"prompts": [_prompt_spec_to_dict(s) for s in _PROMPT_SPECS]}


@app.patch("/prompts")
def update_prompt(request: PromptUpdateRequest, config=Depends(get_config)):
    from openhachimi_agent.content.prompt_registry import get_prompt_spec
    spec = get_prompt_spec(request.name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知的提示词: {request.name}")
    if request.content.strip():
        write_override(request.name, request.content)
        logger.info("http prompt override written name=%s", request.name)
    else:
        delete_override(request.name)  # 不存在也安全(幂等回退)
        logger.info("http prompt override deleted(restored builtin) name=%s", request.name)
    return {
        "name": request.name,
        "content": load_system_prompt(request.name),
        "is_overridden": is_overridden(request.name),
    }


# ------------------------------------------------------------------ WebUI Skills 配置
# GET /skills 返回扫到的技能清单(含单项 disabled 态);PATCH /skills/toggle 写回
# 对应 SKILL.md 的 frontmatter 的 disable-model-invocation。同 /prompts 属"特殊分组",
# 不走 yaml 字段表。find_skills 按 mtime 缓存,文件写回后自动失效重读。


def _is_subpath(path: Path, base: Path) -> bool:
    """判断 path 是否位于 base 之下(含自身)。用于防止 source_path 越权写任意文件。"""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _skill_dir_key(skill_path: Path, skills_dirs: list[Path]) -> str:
    """该 SKILL.md 所属的 skills_dir 标识:首个目录标 "user",其余取目录 basename。"""
    for idx, d in enumerate(skills_dirs):
        try:
            d_resolved = d.resolve()
            if _is_subpath(skill_path.resolve(), d_resolved):
                return "user" if idx == 0 else d.name
        except OSError:
            continue
    return "user"


@app.get("/skills")
def get_skills(config=Depends(get_config)):
    skills = find_skills(config.skills_dirs)
    items = [
        SkillItem(
            name=s.config.name,
            description=s.config.description,
            source_path=str(s.path),
            source_dir_key=_skill_dir_key(s.path, config.skills_dirs),
            disabled=s.config.disable_model_invocation,
            category=s.config.category,
        )
        for s in skills
    ]
    return SkillsResponse(skills=items)


@app.patch("/skills/toggle")
def toggle_skill(request: SkillToggleRequest, config=Depends(get_config)):
    # 安全校验:source_path 必须落在某个受管 skills_dir 下,防越权改任意文件。
    try:
        p = Path(request.source_path).resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc
    if not any(_is_subpath(p, d.resolve()) for d in config.skills_dirs):
        raise HTTPException(status_code=400, detail="技能路径不在受管目录内")
    try:
        set_skill_disable_model_invocation(p, request.disabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc
    logger.info("http skill toggle path=%s disabled=%s", p, request.disabled)
    return SkillToggleResult(source_path=str(p), disabled=request.disabled)


@app.post("/skills/install")
def install_skill(request: SkillInstallRequest, config=Depends(get_config)):
    """从 Git URL / zip-tar 下载 URL / 本地目录 安装或更新技能到 user/skills。

    网络操作可能耗时,同步 handler 跑在 threadpool。复用 tools.skills 的纯逻辑函数,
    含 SSRF 防护、大小/数量限制、staging+backup 回滚。返回结果字符串。
    """
    user_skills_dir = config.user_dir / "skills"
    logger.info("http skill install source=%s allow_http=%s", request.source_path_or_url, request.allow_http)
    message = install_skill_from_source(
        user_skills_dir,
        request.source_path_or_url,
        allow_http=request.allow_http,
    )
    return SkillInstallResult(message=message)


@app.delete("/skills")
def delete_skill(request: SkillDeleteRequest, config=Depends(get_config)):
    """删除 user/skills 下的某个技能目录。仅允许删 user 源技能;外部目录只读。

    通过 source_path(SKILL.md 路径)定位,delete_skill_dir 内部做越权校验。
    """
    try:
        p = Path(request.source_path).resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc
    # 仅允许删 user/skills 下的技能:source_path 必须落在首个 skills_dir(user)内。
    user_dir = config.skills_dirs[0].resolve()
    try:
        p.relative_to(user_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="仅可删除 user 源技能(外部目录为只读)")
    try:
        message = delete_skill_dir(user_dir, p)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc
    logger.info("http skill delete path=%s", p)
    return SkillDeleteResult(source_path=str(p), message=message)


# ------------------------------------------------------------------ WebUI MCP 配置
# GET /mcp 返回 mcp-servers.json 的服务器清单;PUT /mcp 整体覆盖写回(json 无注释,
# 原子写防中途损坏)。type 由 command/url 派生。改后需重启进程(mcp_manager 启动期建连)。


@app.get("/mcp")
def get_mcp_servers(config=Depends(get_config)):
    cfg = get_mcp_config(config.user_dir)
    items = [
        MCPServerItem(
            name=name,
            type=srv.type,
            command=srv.command,
            args=list(srv.args),
            url=srv.url,
            env=srv.env,
            headers=srv.headers,
        )
        for name, srv in cfg.servers.items()
    ]
    return McpServersResponse(servers=items)


@app.put("/mcp")
def update_mcp_servers(request: McpServersUpdateRequest, config=Depends(get_config)):
    servers: dict[str, MCPServerConfig] = {}
    seen: set[str] = set()
    for it in request.servers:
        name = it.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="存在空名称的 MCP 服务器")
        if name in seen:
            raise HTTPException(status_code=400, detail=f"MCP 服务器名称重复: {name}")
        seen.add(name)
        if it.type == "stdio":
            if not it.command or not it.command.strip():
                raise HTTPException(status_code=400, detail=f"stdio 服务器 {name} 缺少 command")
            servers[name] = MCPServerConfig(
                type="stdio", command=it.command, args=list(it.args), env=it.env
            )
        else:
            if not it.url or not it.url.strip():
                raise HTTPException(status_code=400, detail=f"http 服务器 {name} 缺少 url")
            servers[name] = MCPServerConfig(type="http", url=it.url, headers=it.headers)
    try:
        write_mcp_config(config.user_dir, servers)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=safe_error_detail(exc)) from exc
    logger.info("http mcp servers rewritten count=%d", len(servers))
    # 回读返回最新态,供前端刷新表单与 dirty 复位。
    cfg = get_mcp_config(config.user_dir)
    items = [
        MCPServerItem(
            name=name, type=srv.type, command=srv.command, args=list(srv.args),
            url=srv.url, env=srv.env, headers=srv.headers,
        )
        for name, srv in cfg.servers.items()
    ]
    return McpServersResponse(servers=items)


# ------------------------------------------------------------------ WebUI 角色管理
# GET /roles-config 返回全部角色(提示词 .md + 绑定 roles-config.json 合并) + 可勾选的
# skills/mcp 清单,前端一次拿全。PUT /roles-config 整覆盖写:同步维护角色 .md 文件
# (增删改,复用 safe_role_file_path 防越权)与 roles-config.json。同 /mcp 属"特殊分组"。


def _role_available_skills(config) -> list[RoleOption]:
    """当前系统**可绑定**的 skill 清单(name + description),供前端多选。

    已 disable-model-invocation 的 skill 直接不返回——运行期它们本就不注入
    (宏工具不注册、不进技能索引、get_skill_instructions 也拦),所以在设置层
    也不该作为可勾选项出现,避免"看到能勾却绑了无效"的死绑定体验。
    """
    try:
        skills = find_skills(config.skills_dirs)
    except Exception:  # noqa: BLE001  扫描失败不应阻断角色管理页
        logger.debug("find_skills failed in roles-config endpoint", exc_info=True)
        return []
    seen: set[str] = set()
    out: list[RoleOption] = []
    for s in skills:
        name = s.config.name
        if name in seen:
            continue
        if getattr(s.config, "disable_model_invocation", False):
            continue
        seen.add(name)
        out.append(RoleOption(name=name, description=(s.config.description or "").strip()))
    return out


def _role_available_mcp_servers(config) -> list[RoleOption]:
    """当前 mcp-servers.json 的 server 名清单,供前端多选。"""
    cfg = get_mcp_config(config.user_dir)
    return [RoleOption(name=name, description="") for name in cfg.servers]


def _disabled_skill_names(config) -> set[str]:
    """当前被 disable-model-invocation 的 skill 名集合。

    供构造角色绑定时剔除"死绑定"——已禁用的 skill 不应留在任何角色的
    selected_skills 里(运行期它们本就不注入,留着只会让用户误以为绑了)。

    直接扫 find_skills 取禁用名,而非复用 _role_available_skills——后者
    已过滤掉禁用项,这里要的恰恰是禁用项本身。
    """
    try:
        skills = find_skills(config.skills_dirs)
    except Exception:  # noqa: BLE001
        return set()
    return {
        s.config.name for s in skills
        if getattr(s.config, "disable_model_invocation", False)
    }


def _build_roles_config_response(config, *, lenient_prompt: bool) -> RolesConfigResponse:
    """构造 GET /roles-config 与 PUT 回读的统一响应。

    两处读盘 + 组装 items 的逻辑本就重复,抽此共用;同时在此集中剔除
    selected_skills 中已 disabled 的项(死绑定),避免两处各漏。PUT 路径
    刚写完角色 .md、prompt 必可读,故 lenient_prompt=False 直接抛 500;
    GET 路径对读取失败给 500(原行为)。
    """
    roles_config = load_roles_config(config.user_dir)
    role_names = list_role_names(config.roles_dir)
    disabled = _disabled_skill_names(config)
    items: list[RoleBindingItem] = []
    for name in role_names:
        path = safe_role_file_path(config.roles_dir, name)
        try:
            prompt = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            if not lenient_prompt:
                raise HTTPException(status_code=500, detail=safe_error_detail(exc)) from exc
            logger.warning("role prompt read failed in lenient path: %s", name, exc_info=True)
            continue
        b = roles_config.get(name)
        # 剔除已禁用 skill 的死绑定:运行期它们不注入,绑定里留着是误导。
        raw_selected = list(b.selected_skills) if b else []
        selected_skills = [s for s in raw_selected if s not in disabled]
        items.append(
            RoleBindingItem(
                name=name,
                prompt=prompt,
                skills_mode=b.skills_mode if b else "all",
                selected_skills=selected_skills,
                mcp_mode=b.mcp_mode if b else "all",
                selected_mcp_servers=list(b.selected_mcp_servers) if b else [],
            )
        )
    return RolesConfigResponse(
        roles=items,
        available_skills=_role_available_skills(config),
        available_mcp_servers=_role_available_mcp_servers(config),
        default_role=config.default_role_name,
    )


@app.get("/roles-config")
def get_roles_config(config=Depends(get_config)):
    return _build_roles_config_response(config, lenient_prompt=False)


@app.put("/roles-config")
def update_roles_config(request: RolesConfigUpdateRequest, config=Depends(get_config)):
    # 前端校验:名称合法且唯一、提示词非空。
    seen: set[str] = set()
    for it in request.roles:
        try:
            name = validate_role_name(it.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc
        if name in seen:
            raise HTTPException(status_code=400, detail=f"角色名称重复: {name}")
        seen.add(name)
        if not it.prompt.strip():
            raise HTTPException(status_code=400, detail=f"角色 {name} 的提示词不能为空")

    # 删除:既有但本次不再出现的角色(保护 default_role,不删)。
    existing = set(list_role_names(config.roles_dir))
    to_delete = existing - seen
    for name in to_delete:
        if name == config.default_role_name:
            continue
        path = safe_role_file_path(config.roles_dir, name)
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=safe_error_detail(exc)) from exc

    # 写/覆盖角色 .md + 收集绑定记录。
    # 写前剔除已 disabled 的 skill 绑定(双保险):前端虽置灰禁止勾选,
    # 仍防绕过——禁用的 skill 不该进 roles-config.json 造成死绑定。
    disabled = _disabled_skill_names(config)
    bindings: dict = {}
    for it in request.roles:
        name = validate_role_name(it.name)
        path = safe_role_file_path(config.roles_dir, name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(it.prompt.strip() + "\n", encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=safe_error_detail(exc)) from exc
        from openhachimi_agent.core.config import RoleBindingConfig
        bindings[name] = RoleBindingConfig(
            skills_mode=it.skills_mode,
            selected_skills=[s for s in it.selected_skills if s not in disabled],
            mcp_mode=it.mcp_mode,
            selected_mcp_servers=list(it.selected_mcp_servers),
        )

    try:
        write_roles_config(config.user_dir, bindings)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=safe_error_detail(exc)) from exc
    logger.info("http roles-config rewritten count=%d deleted=%d", len(bindings), len(to_delete))

    # 回读最新态返回,供前端刷新表单与 dirty 复位(共用 _build_roles_config_response,
    # 已含读后剔除死绑定)。
    return _build_roles_config_response(config, lenient_prompt=False)


@app.post("/stop")
async def stop_session(request: StopRequest, service: AgentService = Depends(get_service)):
    logger.info("http stop session request session_id=%s", request.session_id)
    return await service.stop_session(request.session_id)


# ------------------------------------------------------------------ WebUI 记忆管理(设置页)
# GET /memory 列出/搜索长期记忆(L1/L2/L3);PATCH /memory/{id} 编辑(仅 L1);
# DELETE /memory 软删除(任意层级)。复用 MemoryStore 原语,HTTP 层独立于 agent 工具。
# 编辑仅限 L1(L2/L3 无 update_*_content 方法);删除为软删除(status=deleted),
# 既不进列表也不进召回。secret 记忆由 list_memories 的 SQL 自动排除(L1)。

_MEMORY_ALL = "__all__"
_HEX_ID_LEN = 32


def _memory_item_from_result(result) -> MemoryItem:
    return MemoryItem(
        id=result.id,
        level=result.level,
        content=result.content,
        memory_type=result.memory_type,
        confidence=float(result.confidence),
        updated_at=result.updated_at,
        score=float(result.score),
        editable=result.level == "L1",
        metadata=result.metadata or {},
    )


def _memory_scope_for_role(role: str | None, config) -> MemoryScope:
    """构造单角色记忆查询 scope。role_name 取传入角色或默认角色。"""
    return MemoryScope(
        tenant_id="local",
        user_id="local",
        role_name=role or config.default_role_name,
        session_id="",
        channel="local",
    )


@app.get("/memory")
def list_memory(
    role: str | None = None,
    q: str | None = None,
    memory_type: str | None = None,
    level: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    include_archived: bool = False,
    config=Depends(get_config),
) -> MemoryListResponse:
    """列出 / 搜索长期记忆。

    role 为空或 ``__all__`` 时跨全部角色合并(遍历磁盘角色清单各查一次后去重);
    否则仅查指定角色(含共享记忆 role_name='')。q 非空走 BM25 搜索,空走 list_memories。
    level / memory_type 在 HTTP 层对结果后置过滤(search 不接受这俩参数)。
    """
    role_names = list_role_names(config.roles_dir)
    stats = {}
    if not config.memory.enabled:
        # 未启用:返回空列表 + enabled=False,PATCH/DELETE 端点会回 503。
        return MemoryListResponse(
            items=[],
            total=0,
            role=role or _MEMORY_ALL,
            roles=role_names,
            default_role=config.default_role_name,
            enabled=False,
            stats={},
        )

    store = get_memory_store(config)
    try:
        stats = store.stats()
    except Exception:  # noqa: BLE001  统计失败不应阻断列表展示
        logger.debug("memory stats failed", exc_info=True)
        stats = {}

    # 跨角色:遍历每个角色的 scope 各查一次后去重;单角色:查一次。
    scopes = (
        [_memory_scope_for_role(r, config) for r in role_names]
        if not role or role == _MEMORY_ALL
        else [_memory_scope_for_role(role, config)]
    )
    # 跨角色 + 空角色记忆兜底:共享记忆 role_name='' 可能不属于任何磁盘角色文件,
    # 但单角色查询的 SQL 已含 ``role_name = ? OR role_name = ''`` 能带到。跨角色场景
    # 任一磁盘角色查询都会带回这些共享记忆,故无需额外查空角色 scope。

    raw = []
    seen_ids: set[str] = set()
    per_role_limit = min(limit, 100)  # list_memories 内部 clamp 到 100
    for scope in scopes:
        try:
            if q and q.strip():
                # search 跨 L1/L2/L3 走 BM25(无效查询词时返回空列表,不抛异常)。
                results = store.search(scope, q.strip(), limit=per_role_limit, include_archived=include_archived, touch_results=False)
            else:
                results = store.list_memories(scope, memory_type=memory_type, limit=per_role_limit, include_archived=include_archived, touch=False)
        except Exception:  # noqa: BLE001  单角色查询失败不影响其他角色
            logger.debug("memory list/search failed role=%s", scope.role_name, exc_info=True)
            continue
        for item in results:
            if item.id in seen_ids:
                continue
            seen_ids.add(item.id)
            raw.append(item)

    # 后置过滤:level / memory_type(search 路径下 list_memories 的 type 过滤未生效)。
    filtered = raw
    if level:
        filtered = [item for item in filtered if item.level == level]
    if memory_type:
        filtered = [item for item in filtered if item.memory_type == memory_type]

    # 按 updated_at DESC 排序后截断到 limit(跨角色合并后需统一排序)。
    filtered.sort(key=lambda item: item.updated_at, reverse=True)
    filtered = filtered[:limit]

    items = [_memory_item_from_result(item) for item in filtered]
    return MemoryListResponse(
        items=items,
        total=len(items),
        role=role or _MEMORY_ALL,
        roles=role_names,
        default_role=config.default_role_name,
        enabled=True,
        stats=stats,
    )


@app.patch("/memory/{memory_id}")
def update_memory(memory_id: str, request: MemoryUpdateRequest, config=Depends(get_config)) -> MemoryUpdateResult:
    """编辑长期记忆(仅 L1 原子)。L2/L3 为只读,会返回 400。"""
    if not config.memory.enabled:
        raise HTTPException(status_code=503, detail="记忆系统未启用")
    store = get_memory_store(config)
    # 用 get_atom_content 探测层级:仅 L1 原子表有此 id 才可编辑。
    if store.get_atom_content(memory_id) is None:
        raise HTTPException(status_code=400, detail="仅 L1 原子记忆可编辑,L2/L3 为只读")
    # 隐私处理:保留 secret 拒绝(防误存明文密钥),关闭 PII 脱敏
    # (管理页是用户主动编辑已存在记忆,自动改手机号会困惑)。
    guard = PrivacyGuard(
        allow_secret_memory=config.memory.privacy.allow_secret_memory,
        pii_redaction=False,
    )
    decision = guard.should_store(request.content)
    if decision.action == "reject":
        raise HTTPException(status_code=400, detail="内容含机密信息,已拒绝")
    embedding_status = "pending" if config.memory.embedding.enabled else "disabled"
    try:
        updated = store.update_atom_content(memory_id, decision.text, embedding_status=embedding_status)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=safe_error_detail(exc)) from exc
    if updated and config.memory.embedding.enabled:
        # 与 tools/memory.py:update_memory 一致:排队让现有 worker 重算向量。
        store.enqueue_unique_job(
            "embed_memory_item",
            {
                "item_id": memory_id,
                "level": "L1",
                "text": decision.text,
                "model": config.memory.embedding.model,
            },
            dedupe_key=f"embed:L1:{memory_id}",
        )
    logger.info("http memory update id=%s updated=%s", memory_id, updated)
    return MemoryUpdateResult(updated=updated, id=memory_id, embedding_status="queued" if updated and config.memory.embedding.enabled else embedding_status)


@app.delete("/memory")
def delete_memory(request: MemoryDeleteRequest, config=Depends(get_config)) -> MemoryDeleteResult:
    """软删除长期记忆(任意层级)。ids 为 32 位 hex 记忆 ID 列表。

    forget 在 ID 路径下 SQL 是 ``UPDATE ... WHERE id=?``,不按 scope 过滤,故 scope
    仅供日志可读。软删后 status=deleted,既不进列表也不进召回,FTS 行一并删除。
    """
    if not config.memory.enabled:
        raise HTTPException(status_code=503, detail="记忆系统未启用")
    # 安全校验:仅接受 32 位 hex ID,拒绝通配符 / 全量删除语义
    # (复刻 tools/memory.py:forget_memory 的约束,防误删)。
    cleaned_ids: list[str] = []
    for raw_id in request.ids:
        sid = raw_id.strip()
        if not sid:
            continue
        if sid in {"*", "%", "all", "ALL"} or set(sid) <= {"*", "%"}:
            raise HTTPException(status_code=400, detail="拒绝通配符删除,请传入明确的记忆 ID")
        if len(sid) != _HEX_ID_LEN or any(c not in "0123456789abcdefABCDEF" for c in sid):
            raise HTTPException(status_code=400, detail=f"无效的记忆 ID: {sid}")
        cleaned_ids.append(sid)
    if not cleaned_ids:
        raise HTTPException(status_code=400, detail="未提供有效的记忆 ID")

    store = get_memory_store(config)
    # scope 仅供 forget 签名一致与日志可读;ID 路径不按 scope 过滤。
    scope = _memory_scope_for_role(None, config)
    try:
        deleted = store.forget(scope, ",".join(cleaned_ids), hard_delete=False)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=safe_error_detail(exc)) from exc
    logger.info("http memory delete ids=%s deleted=%d", cleaned_ids, deleted)
    return MemoryDeleteResult(deleted=deleted, ids=cleaned_ids)


@app.post("/commands")
async def dispatch_command(
    request: CommandDispatchRequest,
    service: AgentService = Depends(get_service),
) -> CommandDispatchResponse:
    """统一的命令分派接口:解析 message,命中即执行并返回结构化结果。

    未命中返回 handled=False,调用方应继续走 /chat 或 /chat/stream。
    """
    logger.info("http command dispatch request message_chars=%d", len(request.message))
    channel_context = {
        "type": "http",
        "platform": "http",
        "session_id": request.session_id,
        "role": request.role,
    }
    outcome = await service.dispatch_command(
        request.message,
        role=request.role,
        session_id=request.session_id,
        channel_context=channel_context,
        channel="http",
    )
    if outcome is None:
        return CommandDispatchResponse(handled=False)
    return CommandDispatchResponse(
        handled=True,
        message=outcome.message,
        kind=outcome.kind,
        role=outcome.role,
        session_id=outcome.session_id,
    )


# ------------------------------------------------------------------ WebUI 静态托管
# 构建产物位于 openhachimi_agent/webui_dist/（见 webui/vite.config.ts）
# 用户 pip install 后会随 wheel 一起发布。若产物不存在（如未跑过 npm build），
# 跳过挂载，访问 /ui/ 会得到 404，不影响其它 API 使用。

from fastapi.staticfiles import StaticFiles  # noqa: E402

_webui_dist = Path(__file__).resolve().parent.parent / "webui_dist"
if _webui_dist.exists():
    # html=True 让 SPA 在路径未命中时回落到 index.html。
    # 配合 vue-router 的 hash 模式（/ui/#/login），刷新不会 404。
    app.mount("/ui", StaticFiles(directory=str(_webui_dist), html=True), name="webui")
    logger.info("webui static dir mounted path=%s", _webui_dist)
else:
    logger.info("webui static dir not found, /ui disabled path=%s", _webui_dist)
