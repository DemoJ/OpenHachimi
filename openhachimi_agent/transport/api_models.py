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


class ScheduleOrigin(BaseModel):
    type: str = "http"
    platform: str = "http"
    session_id: str | None = None
    role: str | None = None


class DeliveryTarget(BaseModel):
    type: str = "inbox"
    chat_id: str | int | None = None
    thread_id: str | int | None = None
    user_id: str | int | None = None
    box: str | None = None


class DeliveryFallback(BaseModel):
    enabled: bool = True
    mode: str = "inbox"
    targets: list[dict[str, Any]] = Field(default_factory=lambda: [{"type": "inbox", "box": "default"}])
    on: list[str] = Field(default_factory=lambda: ["resolve_failed", "send_failed"])


class ScheduleCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    schedule_type: Literal["once", "interval", "cron"]
    schedule_expr: str = Field(min_length=1)
    role: str | None = None
    session_id: str | None = None
    timezone: str = "UTC"
    timeout_seconds: int | None = Field(default=None, gt=0)
    origin: dict[str, Any] | None = None
    delivery_mode: str = "origin"
    delivery_targets: list[dict[str, Any]] | None = None
    delivery_fallback: dict[str, Any] | None = None
    execution_policy: dict[str, Any] | None = None


class ScheduleUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    prompt: str | None = Field(default=None, min_length=1)
    schedule_type: Literal["once", "interval", "cron"] | None = None
    schedule_expr: str | None = Field(default=None, min_length=1)
    role: str | None = None
    session_id: str | None = None
    timezone: str | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    execution_policy: dict[str, Any] | None = None


class ScheduleDeliveryUpdateRequest(BaseModel):
    delivery_mode: str
    delivery_targets: list[dict[str, Any]] | None = None
    delivery_fallback: dict[str, Any] | None = None


class ScheduleResponse(BaseModel):
    id: str
    name: str
    prompt: str
    schedule_type: str
    schedule_expr: str
    timezone: str
    status: str
    enabled: bool
    role: str | None = None
    session_id: str | None = None
    timeout_seconds: int | None = None
    origin: dict[str, Any] = Field(default_factory=dict)
    delivery_mode: str = "origin"
    delivery_targets: list[dict[str, Any]] = Field(default_factory=list)
    delivery_fallback: dict[str, Any] = Field(default_factory=dict)
    execution_policy: dict[str, Any] = Field(default_factory=dict)
    safety_status: str | None = None
    safety_error: str | None = None
    next_run_at: str | None = None
    created_at: str
    updated_at: str
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_delivery_status: str | None = None
    last_delivery_error: str | None = None
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
    delivery_status: str | None = None
    delivery_targets: list[dict[str, Any]] = Field(default_factory=list)
    delivery_results: list[dict[str, Any]] = Field(default_factory=list)
    delivery_error: str | None = None
    delivered_at: str | None = None
    read_at: str | None = None
    safety_status: str | None = None
    safety_error: str | None = None
    execution_context: dict[str, Any] = Field(default_factory=dict)


class DeliveryPreviewResponse(BaseModel):
    mode: str
    targets: list[dict[str, Any]] = Field(default_factory=list)
    fallback: dict[str, Any] = Field(default_factory=dict)
    origin: dict[str, Any] = Field(default_factory=dict)


class CommandResponse(BaseModel):
    message: str
    role: str
    session_id: str


class RolesResponse(BaseModel):
    roles: list[str]
    current_role: str
