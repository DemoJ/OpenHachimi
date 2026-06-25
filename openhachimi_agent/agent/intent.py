"""Intent routing contracts and conservative fallback heuristics."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


TaskKind = Literal[
    "qa",
    "code_change",
    "file_ops",
    "shell",
    "browser",
    "research",
    "unknown",
]
Complexity = Literal["simple", "complex"]
RiskLevel = Literal["low", "medium", "high"]
AllowedAutonomy = Literal["narrow", "bounded", "broad"]
# 历史上还有 "skill_direct"，那是 router 选 skill 时的特殊 mode；现在 skill
# 召回由主模型在 executor 阶段通过 get_skill_instructions 主动决定，不再走
# router → mode 这条耦合，"skill_direct" 退役。老会话 task_frame_json 里如果
# 仍带这个值，pydantic extra="ignore" + 自定义 validator 会把它降级到 "direct"。
ExecutionMode = Literal["direct", "planned"]
PlanContinuationAction = Literal["continue_active_plan", "resume_suspended_plan", "start_new_task"]
_URL_PATTERN = re.compile(r"https?://[^\s<>()\"'，。；、]+", re.IGNORECASE)


_NONE_LITERAL_STRINGS = {"none", "null", "nil", "n/a", "na", "undefined", ""}


def _normalize_optional_str(value: Any) -> str | None:
    """把 LLM 输出里"看起来像 None 的字符串"统一归一到 Python None。

    pydantic_ai 在结构化输出时,有些模型(尤其是 OpenAI 兼容服务)会把可选字段
    输出为字符串字面量 ``"None"`` / ``"null"`` 而不是 JSON ``null``。后续序列化
    走 ``json.dumps`` 时,这会变成可见的 ``"clarifying_question": "None"``,既
    占 token 又让模型误以为有真的追问内容。这里强制清洗为 None。
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in _NONE_LITERAL_STRINGS:
            return None
        return stripped
    return value


def _coerce_legacy_execution_mode(value: Any) -> Any:
    """老会话/老 router 可能输出 "skill_direct"；新模式下退役该值，统一降到 "direct"。

    放在 ``model_validator(mode="before")`` 之前以字段级 validator 形态调用，让
    pydantic 不再因为非法字面量直接抛 ValidationError。
    """
    if isinstance(value, str) and value.strip().lower() == "skill_direct":
        return "direct"
    return value


class PlanContinuationDecision(BaseModel):
    action: PlanContinuationAction = "start_new_task"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = ""


class IntentDecision(BaseModel):
    """Structured router output used by the service layer."""

    # 老会话持久化的 task_frame_json 可能仍含 relevant_skills 等已删字段，忽略以兼容。
    model_config = ConfigDict(extra="ignore")

    task_kind: TaskKind = "unknown"
    complexity: Complexity = "simple"
    risk: RiskLevel = "low"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    requires_plan: bool = False
    requires_user_confirmation: bool = False
    clarifying_question: str | None = None
    target_urls: list[str] = Field(default_factory=list)
    must_preserve_targets: bool = False
    execution_mode: ExecutionMode = "direct"
    rationale: str = ""

    @field_validator("clarifying_question", mode="before")
    @classmethod
    def _clean_clarifying_question(cls, value: Any) -> Any:
        return _normalize_optional_str(value)

    @field_validator("execution_mode", mode="before")
    @classmethod
    def _normalize_execution_mode(cls, value: Any) -> Any:
        return _coerce_legacy_execution_mode(value)


class TargetEntity(BaseModel):
    """A concrete user-specified target that must be preserved across planning and execution."""

    type: Literal["url", "path", "symbol", "text"] = "text"
    value: str
    role: str = "primary"
    immutable: bool = True


class TaskFrame(BaseModel):
    """The task contract shared by routing, planning and execution."""

    # 老会话持久化的 task_frame_json 里仍可能带 relevant_skills（已删除字段）—— 用
    # extra="ignore" 让 pydantic 直接丢弃，不抛 ValidationError。
    model_config = ConfigDict(extra="ignore")

    user_request: str = ""
    goal: str = ""
    task_kind: TaskKind = "unknown"
    complexity: Complexity = "simple"
    risk: RiskLevel = "low"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    requires_plan: bool = False
    requires_user_confirmation: bool = False
    clarifying_question: str | None = None
    target_entities: list[TargetEntity] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    allowed_autonomy: AllowedAutonomy = "bounded"
    execution_mode: ExecutionMode = "direct"
    replan_triggers: list[str] = Field(default_factory=list)
    direct_execution_reason: str = ""
    rationale: str = ""

    @property
    def target_urls(self) -> list[str]:
        return [entity.value for entity in self.target_entities if entity.type == "url"]

    @field_validator("clarifying_question", mode="before")
    @classmethod
    def _clean_clarifying_question(cls, value: Any) -> Any:
        return _normalize_optional_str(value)

    @field_validator("execution_mode", mode="before")
    @classmethod
    def _normalize_execution_mode(cls, value: Any) -> Any:
        return _coerce_legacy_execution_mode(value)


_HIGH_RISK_TERMS = ("删除", "覆盖", "发布", "部署", "reset", "clean", "密钥", "token", "登录")
_SKILL_TERMS = ("skill", "技能")
_SKILL_INSTALL_UPDATE_TERMS = ("install", "update", "add", "import", "安装", "更新", "添加", "导入", "最新版本")


def _is_skill_install_or_update_request(message: str, urls: list[str]) -> bool:
    """Conservatively detect explicit skill install/update requests with a source URL."""

    if not urls:
        return False
    text = message.lower()
    return any(term in text for term in _SKILL_TERMS) and any(
        term in text for term in _SKILL_INSTALL_UPDATE_TERMS
    )


def _skill_install_update_invariant(urls: list[str]) -> str:
    source = urls[0]
    return (
        f"Skill install/update requests with the explicit source URL {source} should prefer "
        f"install_skill(source_path_or_url={source!r}) as the default project-local installer. "
        "install_skill supports updating an already installed skill with the same name. "
        "If the user or the skill documentation requires a command-based update flow, explain "
        "the reason before using command tools."
    )


def extract_urls(message: str) -> list[str]:
    """Extract explicit URLs from a user request."""

    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_PATTERN.finditer(message):
        url = match.group(0).rstrip(".,;:!?)]}")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _target_entities_from_urls(urls: list[str]) -> list[TargetEntity]:
    return [TargetEntity(type="url", value=url, role="primary", immutable=True) for url in urls]


def build_task_frame(message: str, decision: IntentDecision | None = None) -> TaskFrame:
    """Build the shared task contract from a user message and intent decision."""

    decision = decision or classify_intent_heuristic(message)
    allowed_autonomy: AllowedAutonomy = "broad" if decision.complexity == "complex" else "bounded"
    execution_mode: ExecutionMode = "planned" if decision.requires_plan else decision.execution_mode

    replan_triggers = [
        "A tool result contradicts the task frame goal or invariants.",
        "A required target entity is unreachable or unavailable.",
        "The current plan no longer satisfies the user's request.",
    ]

    direct_reason = ""
    if not decision.requires_plan:
        direct_reason = "Task is simple, low risk, and has a concrete target or answer path."

    invariants: list[str] = []
    if decision.risk == "high":
        invariants.append("Ask for confirmation before irreversible, destructive, publishing, deployment, credential, or login-sensitive actions.")

    return TaskFrame(
        user_request=message,
        goal=message.strip(),
        task_kind=decision.task_kind,
        complexity=decision.complexity,
        risk=decision.risk,
        confidence=decision.confidence,
        requires_plan=decision.requires_plan,
        requires_user_confirmation=decision.requires_user_confirmation,
        clarifying_question=decision.clarifying_question,
        target_entities=_target_entities_from_urls(decision.target_urls),
        invariants=invariants,
        allowed_autonomy=allowed_autonomy,
        execution_mode=execution_mode,
        replan_triggers=replan_triggers,
        direct_execution_reason=direct_reason,
        rationale=decision.rationale,
    )


def classify_intent_heuristic(message: str) -> IntentDecision:
    """Router 失败时的保守兜底分类器。

    不试图精确分类任务类型，仅做最基本的风险识别。
    把具体的任务规划和工具选择交给 Executor/Planner 自行决策。
    """

    text = message.strip().lower()
    target_urls = extract_urls(message)
    if not text:
        return IntentDecision(
            task_kind="unknown",
            complexity="simple",
            risk="low",
            confidence=0.0,
            requires_plan=False,
            clarifying_question="你希望我具体处理什么任务？",
            target_urls=[],
            rationale="empty message",
        )

    risk: RiskLevel = "high" if any(term in text for term in _HIGH_RISK_TERMS) else "low"

    return IntentDecision(
        task_kind="unknown",
        complexity="simple",
        risk=risk,
        confidence=0.5,
        requires_plan=False,
        requires_user_confirmation=risk == "high",
        target_urls=target_urls,
        must_preserve_targets=bool(target_urls),
        rationale="heuristic fallback (conservative)",
    )


def coerce_intent_decision(value: object, message: str) -> IntentDecision:
    """Normalize router results while keeping failures conservative."""

    if isinstance(value, IntentDecision):
        return value
    if isinstance(value, str):
        if value == "COMPLEX_TASK":
            decision = classify_intent_heuristic(message)
            decision.complexity = "complex"
            decision.requires_plan = True
            decision.rationale = "legacy router output"
            return decision
        if value == "SIMPLE_TASK":
            decision = classify_intent_heuristic(message)
            decision.complexity = "simple"
            decision.requires_plan = decision.risk == "high"
            decision.rationale = "legacy router output"
            return decision
    try:
        return IntentDecision.model_validate(value)
    except Exception:
        decision = classify_intent_heuristic(message)
        decision.confidence = min(decision.confidence, 0.4)
        decision.requires_plan = decision.risk == "high"
        if decision.requires_plan:
            decision.execution_mode = "planned"
        decision.rationale = "unparseable router output"
        return decision


def coerce_task_frame(value: object, message: str) -> TaskFrame:
    """将 Router 返回结果标准化为 TaskFrame。

    v3: skill 召回退出 TaskFrame 后,这里只做：
    - 兜底 user_request 来自原始消息；
    - 去重 target_entities 里和原句重复的 primary text；
    - 补 LLM 漏掉的显式 URL target_entities；
    - 检测 skill install/update 请求并加 invariants；
    - 同步 execution_mode 与 requires_plan。

    此函数是 TaskFrame 的最后一层防御。
    """

    if isinstance(value, TaskFrame):
        frame = value
    elif isinstance(value, IntentDecision):
        frame = build_task_frame(message, value)
    else:
        try:
            frame = TaskFrame.model_validate(value)
        except Exception:
            frame = build_task_frame(message, coerce_intent_decision(value, message))

    # 1) user_request 兜底:以原始消息为唯一真值(防 LLM 留空)
    if not (frame.user_request and frame.user_request.strip()):
        frame.user_request = message

    # 2) user_request 本身也从 target_entities 中移除冗余
    #    (LLM 可能把原句塞进 primary text entity,这是重复浪费)
    msg_norm = message.strip()
    frame.target_entities = [
        e for e in frame.target_entities
        if not (e.role == "primary" and e.type == "text" and e.value.strip() == msg_norm)
    ]

    # 3) 仅补充 LLM 可能遗漏的显式 URL target_entities
    detected_urls = extract_urls(message)
    if detected_urls:
        existing_urls = set(frame.target_urls)
        for entity in _target_entities_from_urls(detected_urls):
            if entity.value not in existing_urls:
                frame.target_entities.append(entity)

    # 4) skill install/update invariants
    if _is_skill_install_or_update_request(message, detected_urls) and not any("install_skill" in invariant for invariant in frame.invariants):
        frame.invariants.append(_skill_install_update_invariant(detected_urls))

    # 5) 规划策略归一化。
    # Router 偶尔会把 simple + low risk 的任务标成 planned,导致简单问答/单步文件
    # 修改也先跑 planner 并创建 TODO。这里以 complexity/risk 为硬约束:只有复杂或
    # 非低风险任务才允许进入 planned；低风险简单任务统一走 direct。
    if frame.complexity == "simple" and frame.risk == "low":
        frame.requires_plan = False
        frame.execution_mode = "direct"
        if not frame.direct_execution_reason:
            frame.direct_execution_reason = "Simple low-risk task should be executed directly without planner/TODO overhead."
    elif frame.requires_plan:
        frame.execution_mode = "planned"

    return frame
