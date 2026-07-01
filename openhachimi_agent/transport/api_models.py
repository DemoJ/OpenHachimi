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


class ConfigUpdateRequest(BaseModel):
    """WebUI 设置页写回请求。updates 为 dotted path → 值 的映射,仅含本次改动的字段。"""
    updates: dict[str, Any] = Field(default_factory=dict)


class PromptUpdateRequest(BaseModel):
    """WebUI 提示词编辑页写回请求。

    content 为空串(或纯空白)语义为"恢复内置"——后端删除覆盖文件,加载时回退内置默认。
    非空则把 content 写入 user/system_prompts/<name>.md。
    """
    name: str = Field(min_length=1)
    content: str = ""


class MessageItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str                          # 用户实际输入（user）或 Agent 回复（assistant）
    prefix: str = ""                      # 仅 user 消息：运行时注入的上下文前缀（时间/记忆/技能等），可折叠显示
    timestamp: str | None = None          # ISO-8601；user 取 ModelRequest.timestamp，assistant 取 ModelResponse.timestamp
    # 仅 assistant：本轮请求的 token 用量。pydantic_ai 的 ModelResponse.usage 提供，
    # 旧会话或缺失 usage 时为 None。键固定为 ``input`` / ``output`` / ``total``。
    tokens: dict[str, int] | None = None
    # 折叠占位条：非 None 时本条是「折叠条」而非真实消息(role 仍为 user 占位)。
    # 前端据此渲染一张折叠卡片，点击展开时调 GET /sessions/{id}/messages/folded/{compression_id}
    # 取回被折叠的原始消息。content 在折叠条上为空串。
    fold: dict | None = None


class SessionMessagesResponse(BaseModel):
    role: str
    session_id: str
    messages: list[MessageItem] = Field(default_factory=list)
    total: int = 0
    has_more: bool = False
    next_before_turn: int | None = None


# ------------------------------------------------------------------ WebUI Skills 配置(设置页)
# 数据形态:扫到的技能清单 + 每项开关(disable-model-invocation),写回各 SKILL.md
# frontmatter,不走 yaml 字段表。同 /prompts 属"特殊设置分组"。


class SkillItem(BaseModel):
    name: str
    description: str
    source_path: str                  # SKILL.md 绝对路径,前端作为唯一 key 与回写标识
    source_dir_key: str               # 所属 skills_dir 标识("user" 或外部目录名)
    disabled: bool                    # 即 SKILL.md frontmatter 的 disable-model-invocation
    category: str | None = None


class SkillsResponse(BaseModel):
    skills: list[SkillItem] = Field(default_factory=list)


class SkillToggleRequest(BaseModel):
    """单项开关写回请求。disabled=true 表示禁用模型自动调用该技能。"""
    source_path: str = Field(min_length=1)
    disabled: bool


class SkillToggleResult(BaseModel):
    source_path: str
    disabled: bool


class SkillInstallRequest(BaseModel):
    """从 URL/本地路径安装或更新技能。安装目标固定为 user/skills。"""
    source_path_or_url: str = Field(min_length=1)
    allow_http: bool = False


class SkillInstallResult(BaseModel):
    """安装结果——后端把 install_skill_from_source 的结果字符串原样返回。"""
    message: str


class SkillDeleteRequest(BaseModel):
    """删除 user/skills 下的技能(SKILL.md 所在目录)。"""
    source_path: str = Field(min_length=1)


class SkillDeleteResult(BaseModel):
    source_path: str
    message: str


# ------------------------------------------------------------------ WebUI MCP 配置(设置页)
# 数据形态:user/mcp-servers.json 内的动态服务器清单(type=stdio/http 字段不同),
# 整体覆盖写。同 /prompts 属"特殊设置分组"。


class MCPServerItem(BaseModel):
    name: str
    type: Literal["stdio", "http"]
    command: str | None = None        # stdio:可执行命令
    args: list[str] = Field(default_factory=list)        # stdio:命令参数
    url: str | None = None            # http:服务器端点
    env: dict[str, str] | None = None     # stdio:环境变量
    headers: dict[str, str] | None = None # http:请求头


class McpServersResponse(BaseModel):
    servers: list[MCPServerItem] = Field(default_factory=list)


class McpServersUpdateRequest(BaseModel):
    """整体覆盖写请求。前端提交当前完整的服务器列表;后端校验后原子覆盖 mcp-servers.json。"""
    servers: list[MCPServerItem] = Field(default_factory=list)


# ------------------------------------------------------------------ WebUI 角色管理(设置页)
# 数据形态:角色提示词(user/roles/*.md)+ 角色级 skills/MCP 绑定(user/roles-config.json)
# 合并返回。整覆盖写时同步维护角色 .md 文件(增删改)与 roles-config.json。
# 同 /prompts / /mcp 属"特殊设置分组"。


class RoleBindingItem(BaseModel):
    """单个角色:提示词内容 + skills/MCP 绑定配置。

    selected_skills 引用 SKILL.md 的 config.name;selected_mcp_servers 引用
    mcp-servers.json 的 server 名。mode=all 时对应 selected 清单忽略。
    """
    name: str                              # 角色名(= 文件名 stem,受 validate_role_name 约束)
    prompt: str                            # 角色提示词正文(.md 全文)
    skills_mode: Literal["all", "selected"] = "all"
    selected_skills: list[str] = Field(default_factory=list)
    mcp_mode: Literal["all", "selected"] = "all"
    selected_mcp_servers: list[str] = Field(default_factory=list)


class RoleOption(BaseModel):
    """角色管理页多选用:当前系统可勾选的 skill / MCP server 清单(name + 一句话)。

    已 disable-model-invocation 的 skill 不在可勾选集合里(后端 ``_role_available_skills``
    直接过滤),故本模型无需 disabled 标记——返回的都是可绑定的。
    """
    name: str
    description: str = ""


class RolesConfigResponse(BaseModel):
    """GET /roles-config 返回:全部角色(含提示词+绑定) + 可勾选清单,前端一次拿全。"""
    roles: list[RoleBindingItem] = Field(default_factory=list)
    available_skills: list[RoleOption] = Field(default_factory=list)
    available_mcp_servers: list[RoleOption] = Field(default_factory=list)
    default_role: str = "default"


class RolesConfigUpdateRequest(BaseModel):
    """PUT /roles-config 整覆盖写请求。

    前端提交当前完整角色列表;后端据此:
    - 列表里每个角色:写/覆盖 user/roles/<name>.md + 更新 roles-config.json 记录;
    - 不再出现的既有角色(且非 default_role):删 .md + 删 roles-config 记录。
    """
    roles: list[RoleBindingItem] = Field(default_factory=list)


# ------------------------------------------------------------------ WebUI 记忆管理(设置页)
# 数据形态:长期记忆库(SQLite)里 L1 原子/L2 区块/L3 画像的列表与增删改。
# 复用 MemoryStore 原语(list_memories/search/update_atom_content/forget/stats),
# HTTP 层独立于 agent 工具(不依赖 RunContext)。同 /prompts / /mcp / /roles-config
# 属"特殊设置分组",不走 yaml 字段表。编辑仅限 L1;删除为软删除(status=deleted)。


class MemoryItem(BaseModel):
    """单条记忆。L1=原子事实(可编辑),L2=合并区块,L3=用户画像(均只读)。"""
    id: str
    level: Literal["L1", "L2", "L3"]
    content: str                       # L1=原始内容;L2/L3="title：summary"(store 已拼好)
    memory_type: str
    confidence: float = 0.0
    updated_at: str                    # ISO-8601
    score: float = 0.0
    editable: bool = False             # L1→True,L2/L3→False(HTTP 层按 level 派生)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryListResponse(BaseModel):
    """GET /memory 返回:记忆列表 + 角色筛选清单 + 库统计,前端一次拿全。"""
    items: list[MemoryItem] = Field(default_factory=list)
    total: int = 0                     # 本页条数(list_memories 无 offset,非全量总数)
    role: str = ""                     # 回显本次查询的 role("__all__" 或具体角色名)
    roles: list[str] = Field(default_factory=list)   # 可选角色清单,前端筛选下拉用
    default_role: str = "default"
    enabled: bool = True               # config.memory.enabled
    stats: dict[str, Any] = Field(default_factory=dict)


class MemoryUpdateRequest(BaseModel):
    """PATCH /memory/{id} 写回请求。仅 L1 原子记忆可编辑。"""
    content: str = Field(min_length=1)


class MemoryUpdateResult(BaseModel):
    updated: bool
    id: str
    embedding_status: str              # pending(已排队重算)/disabled(未启用 embedding)


class MemoryDeleteRequest(BaseModel):
    """DELETE /memory 软删除请求。ids 为 32 位 hex 记忆 ID 列表。"""
    ids: list[str] = Field(min_length=1)


class MemoryDeleteResult(BaseModel):
    deleted: int
    ids: list[str] = Field(default_factory=list)
