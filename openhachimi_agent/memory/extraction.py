"""长期记忆抽取。"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.capture import _keywords
from openhachimi_agent.memory.models import ExtractedMemory, MemoryExtractionResult, MemoryScope, MemoryStability
from openhachimi_agent.memory.privacy import PrivacyGuard
from openhachimi_agent.memory.prompts import MEMORY_EXTRACTION_PROMPT

logger = logging.getLogger(__name__)

PREFERENCE_MARKERS = ("记住", "以后", "偏好", "喜欢", "不喜欢", "prefer", "preference", "like", "dislike")
CONSTRAINT_MARKERS = ("必须", "不要", "只能", "要求", "禁止", "must", "should not", "do not")
PROJECT_MARKERS = ("项目", "仓库", "技术栈", "决定", "背景", "架构", "使用")
DECISION_MARKERS = ("决定", "选型", "采用", "放弃", "decision", "decide", "choose")
TEMPORARY_MARKERS = ("今天", "临时", "本周", "这次", "temporary", "today", "this week")
STABLE_MARKERS = ("以后", "长期", "总是", "偏好", "习惯", "要求", "always", "prefer", "preference")
DISLIKE_MARKERS = ("不喜欢", "不要", "禁止", "dislike", "do not", "should not")
IMPLICIT_PREFERENCE_MARKERS = ("改成", "调整为", "设为", "默认", "字体", "字号", "颜色", "主题", "format", "font", "theme", "default")


def extract_memories_from_turn(
    user_message: str,
    assistant_output: str,
    scope: MemoryScope,
    turn_id: str,
    *,
    privacy_guard: PrivacyGuard | None = None,
    config: AppConfig | None = None,
) -> MemoryExtractionResult:
    text = user_message.strip()
    if not text:
        return MemoryExtractionResult()
    guard = privacy_guard or PrivacyGuard()
    decision = guard.should_store(text)
    if decision.action == "reject":
        return MemoryExtractionResult()
    content = decision.text.strip()
    llm_result = _extract_with_llm(content, assistant_output, config) if config else MemoryExtractionResult()
    rule_result = _extract_with_rules(content, guard)
    return _merge_results(llm_result, rule_result)


def _extract_with_llm(user_message: str, assistant_output: str, config: AppConfig | None) -> MemoryExtractionResult:
    if not config or not config.openai_api_key:
        return MemoryExtractionResult()
    base_url = config.openai_base_url.strip()
    if not base_url:
        return MemoryExtractionResult()
    payload = {
        "model": config.model_name,
        "messages": [
            {"role": "system", "content": _llm_extraction_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {"user_message": user_message, "assistant_output": assistant_output},
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.openai_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=config.memory.capture.extract_timeout_seconds) as response:
            raw = response.read()
        data = json.loads(raw.decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        memories = [_extracted_from_json(item) for item in parsed.get("memories", []) if isinstance(item, dict)]
        logger.info("memory llm extraction succeeded memories=%d duration_ms=%d", len(memories), int((time.perf_counter() - started) * 1000))
        return MemoryExtractionResult(memories=[memory for memory in memories if memory is not None])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        logger.warning("memory llm extraction degraded: %s", exc)
        return MemoryExtractionResult()


def _extract_with_rules(content: str, guard: PrivacyGuard) -> MemoryExtractionResult:
    lowered = content.lower()
    memory_type = _memory_type(content, lowered)
    if memory_type is None:
        return MemoryExtractionResult()
    now = datetime.now(timezone.utc)
    stability = MemoryStability.STABLE if _contains_any(content, lowered, STABLE_MARKERS) else MemoryStability.SITUATIONAL
    valid_until = None
    decay_at = None
    if _contains_any(content, lowered, TEMPORARY_MARKERS):
        stability = MemoryStability.EPHEMERAL
        valid_until = (now + timedelta(days=7)).isoformat()
        decay_at = (now + timedelta(days=1)).isoformat()
    elif stability == MemoryStability.SITUATIONAL:
        decay_at = (now + timedelta(days=30)).isoformat()
    extracted = ExtractedMemory(
        memory_type=memory_type,
        content=content,
        subject="user",
        predicate="states",
        object=content,
        keywords=_keywords(content),
        tags=_tags_for(content, lowered, memory_type),
        confidence=0.86 if stability == MemoryStability.STABLE else 0.74,
        stability=stability,
        sensitivity=guard.should_store(content).sensitivity,
        source_quote=content[:500],
    )
    setattr(extracted, "valid_until", valid_until)
    setattr(extracted, "decay_at", decay_at)
    return MemoryExtractionResult(memories=[extracted])


def _memory_type(text: str, lowered: str) -> str | None:
    if _contains_any(text, lowered, CONSTRAINT_MARKERS):
        return "constraint"
    if _contains_any(text, lowered, PREFERENCE_MARKERS) or _contains_any(text, lowered, IMPLICIT_PREFERENCE_MARKERS):
        return "preference"
    if _contains_any(text, lowered, DECISION_MARKERS):
        return "decision"
    if _contains_any(text, lowered, PROJECT_MARKERS):
        return "project_context"
    return None


def _tags_for(text: str, lowered: str, memory_type: str) -> list[str]:
    tags = [memory_type]
    if _contains_any(text, lowered, DISLIKE_MARKERS):
        tags.append("dislike")
    if _contains_any(text, lowered, TEMPORARY_MARKERS):
        tags.append("temporary")
    if _contains_any(text, lowered, IMPLICIT_PREFERENCE_MARKERS):
        tags.append("implicit")
    return tags


def _contains_any(text: str, lowered: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text or marker in lowered for marker in markers)


def _llm_extraction_prompt() -> str:
    return (
        MEMORY_EXTRACTION_PROMPT
        + "\n请从输入 JSON 中抽取长期记忆，返回严格 JSON："
        + '{"memories":[{"memory_type":"preference|constraint|project_context|decision|fact|workflow",'
        + '"content":"...","subject":"user","predicate":"states","object":"...",'
        + '"keywords":["..."],"entities":["..."],"tags":["..."],'
        + '"confidence":0.0,"stability":"ephemeral|situational|stable","source_quote":"..."}]}。'
        + "要识别隐含偏好，例如用户反复要求字体/格式/语言/流程选择，即使没有出现‘偏好’二字。无长期价值时返回空数组。"
    )


def _extracted_from_json(item: dict[str, Any]) -> ExtractedMemory | None:
    memory_type = str(item.get("memory_type", "fact")).strip() or "fact"
    content = str(item.get("content", "")).strip()
    if not content:
        return None
    stability_value = str(item.get("stability", "situational"))
    try:
        stability = MemoryStability(stability_value)
    except ValueError:
        stability = MemoryStability.SITUATIONAL
    confidence = float(item.get("confidence", 0.7))
    return ExtractedMemory(
        memory_type=memory_type,
        content=content,
        subject=str(item.get("subject", "user")),
        predicate=str(item.get("predicate", "states")),
        object=str(item.get("object", content)),
        keywords=[str(value)[:32] for value in item.get("keywords", []) if str(value).strip()][:12],
        entities=[str(value)[:64] for value in item.get("entities", []) if str(value).strip()][:12],
        tags=[str(value)[:32] for value in item.get("tags", []) if str(value).strip()][:12],
        confidence=max(0.0, min(1.0, confidence)),
        stability=stability,
        source_quote=str(item.get("source_quote", content))[:500],
    )


def _merge_results(primary: MemoryExtractionResult, fallback: MemoryExtractionResult) -> MemoryExtractionResult:
    memories: list[ExtractedMemory] = []
    seen: set[tuple[str, str]] = set()
    for memory in [*primary.memories, *fallback.memories]:
        key = (memory.memory_type, memory.content.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        memories.append(memory)
    return MemoryExtractionResult(memories=memories, persona_updates=primary.persona_updates or fallback.persona_updates)
