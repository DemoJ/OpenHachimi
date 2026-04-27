"""localhost HTTP daemon。"""

from fastapi import FastAPI, HTTPException

from openhachimi_agent.api_models import ChatRequest, RoleSwitchRequest
from openhachimi_agent.config import load_config
from openhachimi_agent.service import AgentService


config = load_config()
service = AgentService(config)
app = FastAPI(title="OpenHachimi Agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/state")
def state():
    return service.state()


@app.get("/roles")
def roles():
    return service.list_roles()


@app.post("/chat")
def chat(request: ChatRequest):
    try:
        return service.send_message(request.message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/new")
def new_session():
    return service.new_session()


@app.post("/role")
def switch_role(request: RoleSwitchRequest):
    try:
        return service.switch_role(request.role.strip())
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
