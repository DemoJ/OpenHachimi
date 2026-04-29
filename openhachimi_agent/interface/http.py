"""localhost HTTP daemon。"""

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from openhachimi_agent.app_logging import configure_logging
from openhachimi_agent.core.config import load_config
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.transport.api_models import ChatRequest, RoleSwitchRequest


config = load_config()
configure_logging(config)
logger = logging.getLogger(__name__)
service = AgentService(config)
app = FastAPI(title="OpenHachimi Agent")
logger.info("server module initialized")


@app.get("/health")
def health() -> dict[str, str]:
    from openhachimi_agent.core.version import get_version

    logger.debug("health check")
    return {"status": "ok", "version": get_version()}


@app.get("/state")
def state():
    return service.state()


@app.get("/roles")
def roles():
    return service.list_roles()


@app.post("/chat")
def chat(request: ChatRequest):
    logger.info("http chat request message_chars=%d stream=false", len(request.message))
    try:
        return service.send_message(request.message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/chat/stream")
def chat_stream(request: ChatRequest):
    logger.info("http chat request message_chars=%d stream=true", len(request.message))
    try:
        return StreamingResponse(
            service.stream_message(request.message),
            media_type="text/plain; charset=utf-8",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/new")
def new_session():
    logger.info("http new session request")
    return service.new_session()


@app.post("/role")
def switch_role(request: RoleSwitchRequest):
    logger.info("http switch role request role=%s", request.role.strip())
    try:
        return service.switch_role(request.role.strip())
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
