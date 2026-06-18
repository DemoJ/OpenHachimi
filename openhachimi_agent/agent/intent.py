"""Intent routing contracts and conservative fallback heuristics."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field


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
    relevant_skills: list[str] = Field(default_factory=list)
    execution_mode: ExecutionMode = "direct"
    replan_triggers: list[str] = Field(default_factory=list)
    direct_execution_reason: str = ""
    rationale: str = ""

    @property
    def target_urls(self) -> list[str]:
        return [entity.value for entity in self.target_entities if entity.type == "url"]


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
        relevant_skills=decision.relevant_skills,
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

    只做结构修复和 target_entities 补充，不覆盖 LLM 对
    task_kind / complexity / requires_plan 等字段的判断。
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

    # 仅补充 LLM 可能遗漏的显式 URL target_entities
    detected_urls = extract_urls(message)
    if detected_urls:
        existing_urls = set(frame.target_urls)
        for entity in _target_entities_from_urls(detected_urls):
            if entity.value not in existing_urls:
                frame.target_entities.append(entity)
    if _is_skill_install_or_update_request(message, detected_urls) and not any("install_skill" in invariant for invariant in frame.invariants):
        frame.invariants.append(_skill_install_update_invariant(detected_urls))
    if frame.requires_plan:
        frame.execution_mode = "planned"
    elif frame.relevant_skills and frame.execution_mode == "direct":
        frame.execution_mode = "skill_direct"
    return frame

