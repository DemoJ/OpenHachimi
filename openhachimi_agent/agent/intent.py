"""Intent routing contracts and conservative fallback heuristics."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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
ExecutionMode = Literal["direct", "skill_direct", "planned"]
PlanContinuationAction = Literal["continue_active_plan", "resume_suspended_plan", "start_new_task"]
SelfCritiqueVerdict = Literal["pass", "revise"]
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


class SkillMatch(BaseModel):
    """Router 输出的命中技能 + 置信度。

    放入 ``TaskFrame.relevant_skills`` 时,可同时兼容 ``list[str]`` 与
    ``list[SkillMatch]`` 两种历史/新格式;参见 ``TaskFrame._coerce_relevant_skills``
    校验器。
    """

    name: str
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    reason: str = ""

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class PlanContinuationDecision(BaseModel):
    action: PlanContinuationAction = "start_new_task"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = ""


class SelfCritiqueDecision(BaseModel):
    """Final-answer review result used before returning an executor response."""

    verdict: SelfCritiqueVerdict = "pass"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)
    repair_instructions: str = ""
    rationale: str = ""


class IntentDecision(BaseModel):
    """Structured router output used by the service layer."""

    task_kind: TaskKind = "unknown"
    complexity: Complexity = "simple"
    risk: RiskLevel = "low"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    requires_plan: bool = False
    requires_user_confirmation: bool = False
    clarifying_question: str | None = None
    target_urls: list[str] = Field(default_factory=list)
    must_preserve_targets: bool = False
    relevant_skills: list[str] = Field(default_factory=list)
    execution_mode: ExecutionMode = "direct"
    rationale: str = ""

    @field_validator("clarifying_question", mode="before")
    @classmethod
    def _clean_clarifying_question(cls, value: Any) -> Any:
        return _normalize_optional_str(value)


class TargetEntity(BaseModel):
    """A concrete user-specified target that must be preserved across planning and execution."""

    type: Literal["url", "path", "symbol", "text"] = "text"
    value: str
    role: str = "primary"
    immutable: bool = True


class TaskFrame(BaseModel):
    """The task contract shared by routing, planning and execution."""

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
    relevant_skills: list[SkillMatch] = Field(default_factory=list)
    execution_mode: ExecutionMode = "direct"
    replan_triggers: list[str] = Field(default_factory=list)
    direct_execution_reason: str = ""
    rationale: str = ""

    @property
    def target_urls(self) -> list[str]:
        return [entity.value for entity in self.target_entities if entity.type == "url"]

    @property
    def relevant_skill_names(self) -> list[str]:
        return [match.name for match in self.relevant_skills if match.name]

    @field_validator("clarifying_question", mode="before")
    @classmethod
    def _clean_clarifying_question(cls, value: Any) -> Any:
        return _normalize_optional_str(value)

    @field_validator("relevant_skills", mode="before")
    @classmethod
    def _coerce_relevant_skills(cls, value: Any) -> Any:
        """兼容 LLM 输出 ``["a", "b"]`` 旧格式;统一归一为 list[SkillMatch]。"""
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        coerced: list[Any] = []
        for item in value:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    coerced.append({"name": name, "confidence": 0.8})
            elif isinstance(item, dict):
                # 字典形态保留为 dict,交由 pydantic 解析为 SkillMatch
                if item.get("name"):
                    coerced.append(item)
            else:
                coerced.append(item)
        return coerced


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
    execution_mode = decision.execution_mode
    if decision.requires_plan:
        execution_mode = "planned"
    elif decision.relevant_skills:
        execution_mode = "skill_direct"

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
        # IntentDecision 仍然是 list[str]（旧路径，例如 heuristic 兜底），让
        # SkillMatch 校验器把它升级为 SkillMatch；置信度按 0.8 兜底。
        relevant_skills=[{"name": name, "confidence": 0.8} for name in decision.relevant_skills],
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
        relevant_skills=[],
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

    v2: 不仅做结构修复和 target_entities 补充,还强行归一化 user_request、
    剔除 target_entities 中的重复 primary text、修正 clarifying_question 字面量
    "None" 字符串。task_kind / complexity / requires_plan 等 LLM 判断保留不动。

    此函数是 TaskFrame 的最后一层防御(防御层结构见修复 2+7)。
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

    # 1) user_request 兜底:以原始消息为唯一真值(修复 [问题2] —— LLM 可能留空)
    if not (frame.user_request and frame.user_request.strip()):
        frame.user_request = message

    # 2) user_request 本身也从 target_entities 中移除冗余
    #    (LLM 可能把原句塞进 primary text entity,这是重复浪费)
    msg_norm = message.strip()
    frame.target_entities = [
        e for e in frame.target_entities
        if not (e.role == "primary" and e.type == "text" and e.value.strip() == msg_norm)
    ]

    # 3) 仅补充 LLM 可能遗漏的显式 URL target_entities(原有逻辑保留)
    detected_urls = extract_urls(message)
    if detected_urls:
        existing_urls = set(frame.target_urls)
        for entity in _target_entities_from_urls(detected_urls):
            if entity.value not in existing_urls:
                frame.target_entities.append(entity)

    # 4) skill install/update 相关不变(已有)
    if _is_skill_install_or_update_request(message, detected_urls) and not any("install_skill" in invariant for invariant in frame.invariants):
        frame.invariants.append(_skill_install_update_invariant(detected_urls))

    # 5) execution_mode 覆盖(已有)
    if frame.requires_plan:
        frame.execution_mode = "planned"
    elif frame.relevant_skills and frame.execution_mode == "direct":
        frame.execution_mode = "skill_direct"

    return frame

