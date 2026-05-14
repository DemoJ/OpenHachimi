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
_URL_PATTERN = re.compile(r"https?://[^\s<>()\"'，。；、]+", re.IGNORECASE)


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
    replan_triggers: list[str] = Field(default_factory=list)
    direct_execution_reason: str = ""
    rationale: str = ""

    @property
    def target_urls(self) -> list[str]:
        return [entity.value for entity in self.target_entities if entity.type == "url"]


_CODE_CHANGE_TERMS = (
    "修改",
    "实现",
    "修复",
    "重构",
    "新增",
    "删除",
    "改代码",
    "补测试",
    "commit",
    "pr",
)
_RESEARCH_TERMS = ("分析", "调研", "审查", "评估", "对比", "总结", "找出", "哪里", "建议")
_SHELL_TERMS = ("运行", "执行命令", "安装", "启动", "测试", "pytest", "npm", "pip")
_BROWSER_TERMS = ("网页", "浏览器", "打开", "点击", "登录", "爬取")
_HIGH_RISK_TERMS = ("删除", "覆盖", "发布", "部署", "reset", "clean", "密钥", "token", "登录")
_SIMPLE_URL_ACTION_TERMS = ("访问", "打开", "看一下", "浏览", "进入")
_COMPLEX_URL_ANALYSIS_TERMS = ("深度", "多个页面", "全站", "爬取", "批量", "对比", "监控")


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


def _invariants_for_decision(message: str, decision: IntentDecision) -> list[str]:
    invariants: list[str] = []
    if decision.target_urls:
        invariants.append(
            "Explicit user-provided URLs are target entities, not search keywords; preserve them exactly unless the user asks to change them."
        )
        for url in decision.target_urls:
            invariants.append(f"Do not replace target URL {url} with a search result, similar site, guessed URL, or alternate domain.")
    if decision.task_kind in {"code_change", "file_ops"}:
        invariants.append("Keep edits scoped to files and behavior needed for the user's request.")
    if decision.risk == "high":
        invariants.append("Ask for confirmation before irreversible, destructive, publishing, deployment, credential, or login-sensitive actions.")
    return invariants


def build_task_frame(message: str, decision: IntentDecision | None = None) -> TaskFrame:
    """Build the shared task contract from a user message and intent decision."""

    decision = decision or classify_intent_heuristic(message)
    allowed_autonomy: AllowedAutonomy = "bounded"
    if decision.task_kind == "browser" and decision.target_urls and decision.complexity == "simple":
        allowed_autonomy = "narrow"
    elif decision.complexity == "complex":
        allowed_autonomy = "broad"

    replan_triggers = [
        "A tool result contradicts the task frame goal or invariants.",
        "A required target entity is unreachable or unavailable.",
        "The current plan no longer satisfies the user's request.",
    ]
    if decision.target_urls:
        replan_triggers.append("The agent is about to substitute, search around, or navigate away from a user-provided primary URL before observing it.")

    direct_reason = ""
    if not decision.requires_plan:
        direct_reason = "Task is simple, low risk, and has a concrete target or answer path."

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
        invariants=_invariants_for_decision(message, decision),
        allowed_autonomy=allowed_autonomy,
        replan_triggers=replan_triggers,
        direct_execution_reason=direct_reason,
        rationale=decision.rationale,
    )


def classify_intent_heuristic(message: str) -> IntentDecision:
    """Fallback classifier used when the LLM router is unavailable."""

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

    task_kind: TaskKind = "qa"
    if target_urls and any(term in text for term in _SIMPLE_URL_ACTION_TERMS):
        task_kind = "browser"
    elif any(term in text for term in _CODE_CHANGE_TERMS):
        task_kind = "code_change"
    elif any(term in text for term in _SHELL_TERMS):
        task_kind = "shell"
    elif any(term in text for term in _BROWSER_TERMS):
        task_kind = "browser"
    elif any(term in text for term in _RESEARCH_TERMS):
        task_kind = "research"

    risk: RiskLevel = "high" if any(term in text for term in _HIGH_RISK_TERMS) else "low"
    wordish_count = len(text.replace("，", " ").replace(",", " ").split())
    simple_targeted_url_task = bool(target_urls) and task_kind == "browser" and not any(
        term in text for term in _COMPLEX_URL_ANALYSIS_TERMS
    )
    complexity: Complexity = "complex" if (
        not simple_targeted_url_task and (task_kind in {"code_change", "research"} or wordish_count > 20)
    ) else "simple"
    requires_plan = complexity == "complex" or risk == "high"

    return IntentDecision(
        task_kind=task_kind,
        complexity=complexity,
        risk=risk,
        confidence=0.55,
        requires_plan=requires_plan,
        requires_user_confirmation=risk == "high",
        target_urls=target_urls,
        must_preserve_targets=bool(target_urls),
        rationale="heuristic fallback",
    )


def coerce_intent_decision(value: object, message: str) -> IntentDecision:
    """Normalize router results while keeping failures conservative."""

    if isinstance(value, IntentDecision):
        return value
    if isinstance(value, str):
        if value == "COMPLEX_TASK":
            decision = classify_intent_heuristic(message)
            if not (decision.task_kind == "browser" and decision.target_urls and decision.risk != "high"):
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
        decision = IntentDecision.model_validate(value)
        heuristic = classify_intent_heuristic(message)
        if heuristic.target_urls:
            decision.target_urls = heuristic.target_urls
            decision.must_preserve_targets = True
            if heuristic.task_kind == "browser" and heuristic.complexity == "simple" and decision.risk != "high":
                decision.task_kind = "browser"
                decision.complexity = "simple"
                decision.requires_plan = False
        return decision
    except Exception:
        decision = classify_intent_heuristic(message)
        decision.confidence = min(decision.confidence, 0.4)
        decision.requires_plan = True
        decision.rationale = "unparseable router output"
        return decision


def coerce_task_frame(value: object, message: str) -> TaskFrame:
    """Normalize router results into a task frame, with heuristic repair for concrete targets."""

    if isinstance(value, TaskFrame):
        frame = value
    elif isinstance(value, IntentDecision):
        frame = build_task_frame(message, value)
    else:
        try:
            frame = TaskFrame.model_validate(value)
        except Exception:
            frame = build_task_frame(message, coerce_intent_decision(value, message))

    heuristic = classify_intent_heuristic(message)
    if heuristic.target_urls:
        existing_urls = set(frame.target_urls)
        for entity in _target_entities_from_urls(heuristic.target_urls):
            if entity.value not in existing_urls:
                frame.target_entities.append(entity)
        frame.invariants = list(dict.fromkeys(frame.invariants + _invariants_for_decision(message, heuristic)))
        if heuristic.task_kind == "browser" and heuristic.complexity == "simple" and frame.risk != "high":
            frame.task_kind = "browser"
            frame.complexity = "simple"
            frame.requires_plan = False
            frame.allowed_autonomy = "narrow"
            frame.direct_execution_reason = "Simple explicit-URL browser task; execute directly without open-ended planning."
    return frame
