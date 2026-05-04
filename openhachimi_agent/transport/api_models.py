"""本地 HTTP 服务的数据模型。"""

from pydantic import BaseModel, Field


class AgentState(BaseModel):
    model: str
    base_url: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    role: str | None = None
    session_id: str | None = None


class ChatResponse(BaseModel):
    output: str
    role: str
    session_id: str


class RoleSwitchRequest(BaseModel):
    role: str = Field(min_length=1)


class CommandResponse(BaseModel):
    message: str
    role: str
    session_id: str


class RolesResponse(BaseModel):
    roles: list[str]
    current_role: str
