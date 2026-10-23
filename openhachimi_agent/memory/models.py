"""长期记忆数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class MemoryLevel(_StringEnum):
    TURN = "L0"
    ATOM = "L1"
    BLOCK = "L2"
    PROFILE = "L3"


class MemoryStatus(_StringEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"
    EXPIRED = "expired"


class MemorySensitivity(_StringEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class MemoryStability(_StringEnum):
    EPHEMERAL = "ephemeral"
    SITUATIONAL = "situational"
    STABLE = "stable"


# 按稳定性给出的默认有效期:临时记忆 7 天后过期、1 天后开始降权;情境性记忆 30 天后
# 开始降权(长期不用再归档);稳定记忆不设期限。
_EPHEMERAL_VALID_DAYS = 7
_EPHEMERAL_DECAY_DAYS = 1
_SITUATIONAL_DECAY_DAYS = 30


def default_expiry(stability: object, now: datetime | None = None) -> tuple[str | None, str | None]:
    """返回 (valid_until, decay_at) 的 ISO 时间。所有写入路径都用它,保证 stability 真的生效。"""
    current = now or datetime.now(timezone.utc)
    value = str(stability)
    if value == MemoryStability.EPHEMERAL.value:
        return (
            (current + timedelta(days=_EPHEMERAL_VALID_DAYS)).isoformat(),
            (current + timedelta(days=_EPHEMERAL_DECAY_DAYS)).isoformat(),
        )
    if value == MemoryStability.SITUATIONAL.value:
        return None, (current + timedelta(days=_SITUATIONAL_DECAY_DAYS)).isoformat()
    return None, None


class MemoryType(_StringEnum):
    """记忆类型枚举:重构后的类型体系,明确区分用户/项目/任务三个维度。"""

    USER_TRAIT = "user_trait"           # 用户自身属性(职业、技能、角色、身份)
    USER_PREFERENCE = "user_preference" # 用户偏好(喜欢什么、讨厌什么、习惯什么)
    USER_CONSTRAINT = "user_constraint" # 用户约束(必须、禁止、不要、只能)
    PROJECT_FACT = "project_fact"       # 项目事实(技术栈、架构、团队、依赖)
    PROJECT_DECISION = "project_decision" # 项目决策(选型、放弃、变更及原因)
    TASK_REFERENCE = "task_reference"   # 历史任务引用(原子化摘要,目标+动作)
    WORKFLOW = "workflow"               # 工作流程(用户的工作方式、协作模式)


# 旧类型名 → 新类型体系。
_MEMORY_TYPE_ALIASES = {
    "preference": MemoryType.USER_PREFERENCE.value,
    "constraint": MemoryType.USER_CONSTRAINT.value,
    "fact": MemoryType.USER_TRAIT.value,
    "project_context": MemoryType.PROJECT_FACT.value,
    "decision": MemoryType.PROJECT_DECISION.value,
}
_MEMORY_TYPE_VALUES = frozenset(item.value for item in MemoryType)


# 常驻记忆类型:关于"用户是谁、要我怎么做"的记忆,每轮都直接放进系统指令,不依赖检索命中。
# 含旧类型名,旧库里没归一的记录也能被选上。
CORE_MEMORY_TYPES = frozenset({
    MemoryType.USER_PREFERENCE.value,
    MemoryType.USER_CONSTRAINT.value,
    MemoryType.USER_TRAIT.value,
    MemoryType.WORKFLOW.value,
    "preference",
    "constraint",
    "fact",
})


def normalize_memory_type(value: object, default: str | None = None) -> str | None:
    """把记忆类型归一到 MemoryType 取值:先映射旧别名,不认识的返回 default。"""
    text = str(value or "").strip().lower()
    text = _MEMORY_TYPE_ALIASES.get(text, text)
    return text if text in _MEMORY_TYPE_VALUES else default


class MemoryJobStatus(_StringEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY = "retry"


@dataclass(frozen=True)
class MemoryScope:
    tenant_id: str = "local"
    user_id: str = "local"
    role_name: str = "default"
    session_id: str = ""
    channel: str = "cli"

    def to_json_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "role_name": self.role_name,
            "session_id": self.session_id,
            "channel": self.channel,
        }


@dataclass
class MemoryTurn:
    tenant_id: str
    user_id: str
    role_name: str
    session_id: str
    channel: str
    user_message: str
    assistant_output: str
    id: str = field(default_factory=lambda: uuid4().hex)
    turn_id: str = field(default_factory=lambda: uuid4().hex)
    tool_calls_summary_json: str = "[]"
    task_frame_json: str = "{}"
    memory_context_ids_json: str = "[]"
    status: str = "completed"
    error_summary: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = field(default_factory=utc_now_iso)
    duration_ms: int = 0
    raw_messages_json_ref: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    source: str = "user"  # "user" | "scheduled" | "system"


@dataclass
class MemoryAtom:
    memory_type: str
    content: str
    scope: MemoryScope
    id: str = field(default_factory=lambda: uuid4().hex)
    subject: str = "user"
    predicate: str = "states"
    object: str = ""
    normalized_content: str = ""
    search_text: str = ""
    evidence_turn_ids: list[str] = field(default_factory=list)
    source_quote: str = ""
    entities: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.7
    stability: MemoryStability = MemoryStability.SITUATIONAL
    sensitivity: MemorySensitivity = MemorySensitivity.PERSONAL
    valid_from: str | None = None
    valid_until: str | None = None
    decay_at: str | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    embedding_status: str = "pending"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    last_accessed_at: str | None = None
    access_count: int = 0


@dataclass
class MemoryBlock:
    block_type: str
    title: str
    summary: str
    scope: MemoryScope
    id: str = field(default_factory=lambda: uuid4().hex)
    details: str = ""
    search_text: str = ""
    atom_ids: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.7
    coherence_score: float = 0.0
    freshness_score: float = 0.0
    status: MemoryStatus = MemoryStatus.ACTIVE
    last_consolidated_at: str | None = None
    embedding_status: str = "pending"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    last_accessed_at: str | None = None
    access_count: int = 0


@dataclass
class MemoryProfile:
    profile_type: str
    tenant_id: str
    user_id: str
    title: str
    summary: str
    id: str = field(default_factory=lambda: uuid4().hex)
    role_name: str | None = None
    traits: dict[str, Any] = field(default_factory=dict)
    preferences: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    dislikes: dict[str, Any] = field(default_factory=dict)
    evidence_atom_ids: list[str] = field(default_factory=list)
    confidence: float = 0.7
    stability: MemoryStability = MemoryStability.STABLE
    status: MemoryStatus = MemoryStatus.ACTIVE
    last_reviewed_at: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    last_accessed_at: str | None = None
    access_count: int = 0


@dataclass
class MemoryJob:
    job_type: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: uuid4().hex)
    status: MemoryJobStatus = MemoryJobStatus.PENDING
    attempts: int = 0
    max_attempts: int = 3
    run_after: str = field(default_factory=utc_now_iso)
    locked_at: str | None = None
    last_error: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass
class MemorySearchResult:
    id: str
    level: Literal["L1", "L2", "L3"]
    content: str
    score: float
    memory_type: str
    confidence: float
    updated_at: str
    source: str = "hybrid"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryContext:
    scope: MemoryScope
    query: str
    results: list[MemorySearchResult] = field(default_factory=list)
    degraded: bool = False
    reason: str = ""

    @property
    def ids(self) -> list[str]:
        return [result.id for result in self.results]


@dataclass
class ExtractedMemory:
    memory_type: str
    content: str
    subject: str = "user"
    predicate: str = "states"
    object: str = ""
    keywords: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.7
    stability: MemoryStability = MemoryStability.SITUATIONAL
    sensitivity: MemorySensitivity = MemorySensitivity.PERSONAL
    source_quote: str = ""
    valid_until: str | None = None
    decay_at: str | None = None

    @property
    def memory_type_enum(self) -> MemoryType | None:
        """将字符串类型转为枚举,非法类型返回 None。"""
        try:
            return MemoryType(self.memory_type)
        except ValueError:
            return None


@dataclass
class MemoryExtractionResult:
    memories: list[ExtractedMemory] = field(default_factory=list)
    persona_updates: list[ExtractedMemory] = field(default_factory=list)
