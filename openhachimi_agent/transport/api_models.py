"""本地 HTTP 服务的数据模型。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentState(BaseModel):
    model: str
    base_url: str | None = None


class AttachmentRef(BaseModel):
    id: str = Field(min_length=1)
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    local_path: str = Field(min_length=1)
    source: Literal["telegram", "http", "local"] = "local"
    kind: Literal["image", "document", "audio", "video", "unknown"] = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactRef(BaseModel):
    id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    content_type: str | None = None
    size_bytes: int = Field(ge=0)
    local_path: str = Field(min_length=1)
    download_url: str | None = None
    title: str | None = None
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str = Field(default="")
    role: str | None = None
    session_id: str | None = None
    attachments: list[AttachmentRef] = Field(default_factory=list)


class ChatResponse(BaseModel):
    output: str
    role: str
    session_id: str
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class RoleSwitchRequest(BaseModel):
    role: str = Field(min_length=1)


class StopRequest(BaseModel):
    session_id: str = Field(min_length=1)


class ScheduleCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    schedule_type: Literal["once", "interval", "cron"]
    schedule_expr: str = Field(min_length=1)
    role: str | None = None
    session_id: str | None = None
    timezone: str = "UTC"
    enabled: bool = True
    timeout_seconds: int | None = Field(default=None, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScheduleUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    prompt: str | None = Field(default=None, min_length=1)
    schedule_type: Literal["once", "interval", "cron"] | None = None
    schedule_expr: str | None = Field(default=None, min_length=1)
    role: str | None = None
    session_id: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    metadata: dict[str, Any] | None = None


class ScheduleResponse(BaseModel):
    id: str
    name: str
    prompt: str
    schedule_type: str
    schedule_expr: str
    role: str | None = None
    session_id: str | None = None
    timezone: str
    enabled: bool
    next_run_at: str | None = None
    timeout_seconds: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    running: bool = False


class ScheduleRunResponse(BaseModel):
    id: str
    task_id: str
    status: str
    started_at: str
    finished_at: str | None = None
    output: str | None = None
    error: str | None = None
    duration_ms: int | None = None


class CommandResponse(BaseModel):
    message: str
    role: str
    session_id: str


class RolesResponse(BaseModel):
    roles: list[str]
    current_role: str
