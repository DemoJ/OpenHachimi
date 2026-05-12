"""localhost HTTP daemon。

通过 FastAPI lifespan 机制，在服务启动时自动启动 Telegram Bot（若已配置 token），
服务关闭时优雅停止 Bot。所有渠道共享同一 asyncio 事件循环。
"""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import load_config
from openhachimi_agent.interface.telegram import telegram_lifespan
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.transport.api_models import ChatRequest, RoleSwitchRequest, StopRequest

logger = logging.getLogger(__name__)


def get_service(request: Request) -> AgentService:
    return request.app.state.service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 应用生命周期管理器。

    负责在服务启动/停止时，统一管理所有渠道（当前为 Telegram Bot）的生命周期。
    各渠道以异步上下文管理器的形式嵌套，共享同一 asyncio 事件循环，无需额外线程。
    """
    config = load_config()
    configure_logging(config)
    app.state.service = AgentService(config)
    logger.info("server module initialized")

    async with telegram_lifespan(config):
        logger.info("all channels started")
        yield
        logger.info("all channels stopping")


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


@app.post("/chat")
async def chat(request: ChatRequest, service: AgentService = Depends(get_service)):
    logger.info("http chat request message_chars=%d stream=false", len(request.message))
    try:
        return await service.send_message(request.message, request.role, request.session_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/chat/stream")
def chat_stream(request: ChatRequest, service: AgentService = Depends(get_service)):
    logger.info("http chat request message_chars=%d stream=true", len(request.message))

    async def sse_generator():
        try:
            async for chunk in service.stream_message(request.message, request.role, request.session_id):
                yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
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


@app.post("/new")
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
