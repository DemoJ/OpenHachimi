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

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import load_config
from openhachimi_agent.core.redaction import safe_error_detail
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
    DeliveryPreviewResponse,
    MessageItem,
    RoleSwitchRequest,
    ScheduleCreateRequest,
    ScheduleDeliveryUpdateRequest,
    ScheduleResponse,
    ScheduleRunResponse,
    ScheduleUpdateRequest,
    SessionListResponse,
    SessionLoadRequest,
    SessionMessagesResponse,
    SessionSummary,
    StopRequest,
)
from openhachimi_agent.tools.utils import resolve_workspace_path

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
                logger.info("server module initialized")
                logger.info("all channels started")
                yield
                logger.info("all channels stopping")
    finally:
        if scheduler is not None:
            await scheduler.stop()
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
    service: AgentService = Depends(get_service),
) -> SessionListResponse:
    logger.info("http list sessions request role=%s channel=%s", role, channel)
    return SessionListResponse(**service.list_sessions(role, channel=channel))


@app.get("/channels")
def list_channels() -> ChannelListResponse:
    """返回可选渠道枚举,前端筛选下拉用。"""
    from openhachimi_agent.storage.session_meta import CHANNEL_CODES, DEFAULT_CHANNEL

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


@app.post("/role")
def switch_role(request: RoleSwitchRequest, service: AgentService = Depends(get_service)):
    logger.info("http switch role request role=%s", request.role.strip())
    try:
        return service.switch_role(request.role.strip())
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=safe_error_detail(exc)) from exc


@app.post("/stop")
async def stop_session(request: StopRequest, service: AgentService = Depends(get_service)):
    logger.info("http stop session request session_id=%s", request.session_id)
    return await service.stop_session(request.session_id)


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
