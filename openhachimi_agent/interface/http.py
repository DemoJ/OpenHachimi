"""localhost HTTP daemon。

通过 FastAPI lifespan 机制，在服务启动时自动启动 Telegram Bot（若已配置 token），
服务关闭时优雅停止 Bot。所有渠道共享同一 asyncio 事件循环。
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, StreamingResponse

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import load_config
from openhachimi_agent.interface.telegram import telegram_lifespan
from openhachimi_agent.scheduler.store import ScheduledTaskStore
from openhachimi_agent.scheduler.scheduler import TaskScheduler
from openhachimi_agent.scheduler.models import ScheduledRun, ScheduledTask
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.transport.api_models import (
    ChatRequest,
    RoleSwitchRequest,
    ScheduleCreateRequest,
    ScheduleResponse,
    ScheduleRunResponse,
    ScheduleUpdateRequest,
    StopRequest,
)
from openhachimi_agent.tools.utils import resolve_workspace_path

logger = logging.getLogger(__name__)


def get_service(request: Request) -> AgentService:
    return request.app.state.service


def get_schedule_store(request: Request) -> ScheduledTaskStore:
    store = getattr(request.app.state, "schedule_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="定时任务未启用")
    return store


def _dt_text(value) -> str | None:
    return value.isoformat() if value else None


def schedule_response(task: ScheduledTask) -> ScheduleResponse:
    return ScheduleResponse(
        id=task.id,
        name=task.name,
        prompt=task.prompt,
        schedule_type=task.schedule_type.value,
        schedule_expr=task.schedule_expr,
        role=task.role,
        session_id=task.session_id,
        timezone=task.timezone,
        enabled=task.enabled,
        next_run_at=_dt_text(task.next_run_at),
        timeout_seconds=task.timeout_seconds,
        metadata=task.metadata,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        last_run_at=_dt_text(task.last_run_at),
        last_status=task.last_status,
        last_error=task.last_error,
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
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 应用生命周期管理器。

    负责在服务启动/停止时，统一管理所有渠道（当前为 Telegram Bot）的生命周期。
    各渠道以异步上下文管理器的形式嵌套，共享同一 asyncio 事件循环，无需额外线程。
    """
    config = load_config()
    configure_logging(config)
    app.state.service = AgentService(config)
    app.state.schedule_store = None
    scheduler = None
    if config.scheduler.enabled and config.scheduler.db_path is not None:
        app.state.schedule_store = ScheduledTaskStore(config.scheduler.db_path)
        scheduler = TaskScheduler(
            app.state.schedule_store,
            app.state.service,
            poll_interval_seconds=config.scheduler.poll_interval_seconds,
            max_concurrency=config.scheduler.max_concurrency,
            default_timeout_seconds=config.scheduler.default_timeout_seconds,
            claim_lock_seconds=config.scheduler.claim_lock_seconds,
        )
        await scheduler.start()
    logger.info("server module initialized")

    try:
        async with telegram_lifespan(config, app.state.service):
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


app = FastAPI(title="OpenHachimi Agent", lifespan=lifespan)


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
def list_schedules(store: ScheduledTaskStore = Depends(get_schedule_store)) -> list[ScheduleResponse]:
    return [schedule_response(task) for task in store.list_tasks()]


@app.post("/schedules")
def create_schedule(request: ScheduleCreateRequest, store: ScheduledTaskStore = Depends(get_schedule_store)) -> ScheduleResponse:
    try:
        task = store.create_task(
            name=request.name,
            prompt=request.prompt,
            schedule_type=request.schedule_type,
            schedule_expr=request.schedule_expr,
            role=request.role,
            session_id=request.session_id,
            timezone_name=request.timezone,
            enabled=request.enabled,
            timeout_seconds=request.timeout_seconds,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schedule_response(task)


@app.get("/schedules/{task_id}")
def get_schedule(task_id: str, store: ScheduledTaskStore = Depends(get_schedule_store)) -> ScheduleResponse:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return schedule_response(task)


@app.patch("/schedules/{task_id}")
def update_schedule(task_id: str, request: ScheduleUpdateRequest, store: ScheduledTaskStore = Depends(get_schedule_store)) -> ScheduleResponse:
    updates = request.model_dump(exclude_unset=True)
    try:
        task = store.update_task(task_id, **updates)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return schedule_response(task)


@app.delete("/schedules/{task_id}")
def delete_schedule(task_id: str, store: ScheduledTaskStore = Depends(get_schedule_store)) -> dict[str, bool]:
    try:
        store.delete_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    return {"ok": True}


@app.post("/schedules/{task_id}/run")
async def run_schedule_now(
    task_id: str,
    store: ScheduledTaskStore = Depends(get_schedule_store),
    service: AgentService = Depends(get_service),
) -> dict[str, bool]:
    from openhachimi_agent.scheduler.runner import ScheduledTaskRunner

    try:
        task = await asyncio.to_thread(store.claim_task_now, task_id, service.config.scheduler.claim_lock_seconds)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="定时任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    runner = ScheduledTaskRunner(store, service, default_timeout_seconds=service.config.scheduler.default_timeout_seconds)
    await runner.run_task(task, preserve_schedule=True)
    return {"ok": True}


@app.get("/schedules/{task_id}/runs")
def list_schedule_runs(task_id: str, limit: int = 20, store: ScheduledTaskStore = Depends(get_schedule_store)) -> list[ScheduleRunResponse]:
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return [schedule_run_response(run) for run in store.list_runs(task_id, limit)]


@app.post("/chat")
async def chat(request: ChatRequest, service: AgentService = Depends(get_service)):
    logger.info("http chat request message_chars=%d attachment_count=%d stream=false", len(request.message), len(request.attachments))
    try:
        return await service.send_message(request.message, request.role, request.session_id, attachments=request.attachments)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/chat/stream")
def chat_stream(request: ChatRequest, service: AgentService = Depends(get_service)):
    logger.info("http chat request message_chars=%d attachment_count=%d stream=true", len(request.message), len(request.attachments))

    async def sse_generator():
        try:
            async for event in service.stream_events(request.message, request.role, request.session_id, attachments=request.attachments):
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
            yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            logger.exception("stream error")
            yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"

    try:
        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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

def new_session(role: str | None = None, service: AgentService = Depends(get_service)):
    logger.info("http new session request role=%s", role)
    return service.new_session(role)


@app.get("/session/latest")
def latest_session(role: str | None = None, service: AgentService = Depends(get_service)):
    logger.info("http latest session request role=%s", role)
    return service.latest_session(role)


@app.post("/role")
def switch_role(request: RoleSwitchRequest, service: AgentService = Depends(get_service)):
    logger.info("http switch role request role=%s", request.role.strip())
    try:
        return service.switch_role(request.role.strip())
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/stop")
async def stop_session(request: StopRequest, service: AgentService = Depends(get_service)):
    logger.info("http stop session request session_id=%s", request.session_id)
    return await service.stop_session(request.session_id)
