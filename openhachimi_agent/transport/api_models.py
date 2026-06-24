"""本地 HTTP 服务的数据模型。"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentState(BaseModel):
    model: str
    base_url: str | None = None
    mcp_servers: int = 0
    mcp_errors: list[str] = Field(default_factory=list)


class AttachmentRef(BaseModel):
    id: str = Field(min_length=1)
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    local_path: str = Field(min_length=1)
    source: Literal["telegram", "weixin", "http", "local"] = "local"
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
    # 渠道编码。WebUI/CLI/Telegram/微信 各自传入对应平台名,后端用它在
    # sidecar 中绑定会话归属并配合 latest_by_scope 做隔离。WebUI 未传时
    # 由 HTTP 入口兜底为 "webui"。
    channel: Literal["webui", "cli", "telegram", "weixin"] | None = None
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


class CommandDispatchRequest(BaseModel):
    message: str = Field(min_length=1)
    role: str | None = None
    session_id: str | None = None


class CommandDispatchResponse(BaseModel):
    handled: bool                       # 是否命中命令(False 时其它字段无意义)
    message: str = ""
    kind: str = "info"                  # 与 CommandOutcome.kind 对齐
    role: str | None = None
    session_id: str | None = None


class RolesResponse(BaseModel):
    roles: list[str]
    current_role: str


class SessionSummary(BaseModel):
    session_id: str
    role: str
    created_at: str | None = None       # 由 session_id 前缀解析得到的 ISO 时间，无法解析时为 None
    mtime: float                         # 文件 mtime 秒级时间戳
    preview: str = ""                    # 首条用户消息截断片段
    message_count: int = 0
    channel: str = "webui"               # 渠道归属;老会话(无 sidecar)默认归 webui


class SessionListResponse(BaseModel):
    role: str
    sessions: list[SessionSummary] = Field(default_factory=list)
    # 分页元信息:前端用 total 决定 hasMore,limit/offset 回显本次请求参数,方便调试。
    # 老客户端读不到这几个字段也无影响;默认值保证服务端构造时不强制传入。
    total: int = 0
    limit: int | None = None
    offset: int = 0


class ChannelListResponse(BaseModel):
    """前端渠道筛选下拉用:全量渠道枚举 + 默认选中项。"""
    channels: list[str]
    default: str


class SessionLoadRequest(BaseModel):
    role: str | None = None
    session_id: str = Field(min_length=1)


class MessageItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str                          # 用户实际输入（user）或 Agent 回复（assistant）
    prefix: str = ""                      # 仅 user 消息：运行时注入的上下文前缀（时间/记忆/技能等），可折叠显示
    timestamp: str | None = None          # ISO-8601；user 取 ModelRequest.timestamp，assistant 取 ModelResponse.timestamp
    # 仅 assistant：本轮请求的 token 用量。pydantic_ai 的 ModelResponse.usage 提供，
    # 旧会话或缺失 usage 时为 None。键固定为 ``input`` / ``output`` / ``total``。
    tokens: dict[str, int] | None = None


class SessionMessagesResponse(BaseModel):
    role: str
    session_id: str
    messages: list[MessageItem] = Field(default_factory=list)
