"""长期记忆抽取。"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.capture import _keywords, _looks_like_scheduler_payload
from openhachimi_agent.memory.models import (
    ExtractedMemory,
    MemoryExtractionResult,
    MemoryScope,
    MemoryStability,
    default_expiry,
    normalize_memory_type,
)
from openhachimi_agent.memory.llm import run_memory_extraction
from openhachimi_agent.memory.privacy import PrivacyGuard

logger = logging.getLogger(__name__)

# 规则抽取兜底:仅处理明确偏好,且强制原子化拆分。
# 规则抽取不再是主路径(LLM 才是),只作为 LLM 失败时的降级方案。
PREFERENCE_MARKERS = ("记住", "以后", "偏好", "喜欢", "不喜欢", "prefer", "preference", "like", "dislike")
CONSTRAINT_MARKERS = ("必须", "不要", "只能", "要求", "禁止", "must", "should not", "do not")
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
    recent_turns: list[dict[str, str]] | None = None,
) -> MemoryExtractionResult:
    """从一轮对话里抽取长期记忆。LLM 是主路径;只有 LLM 不可用或调用失败时才退回规则抽取。

    LLM 正常返回空列表表示"这轮没有值得记的",此时不再用规则补抽:规则只看关键词,
    会把任务请求里的"不要改动""必须先跑测试"当成长期约束存下来。
    ``recent_turns`` 是同一会话前几轮,只给 LLM 用来理解"好的,就这么办"这类回复。
    """
    text = user_message.strip()
    if not text:
        return MemoryExtractionResult()
    # 防御:即便上游漏过滤,这里仍然拒绝定时任务执行 payload 形态的文本
    if _looks_like_scheduler_payload(text):
        logger.info("memory extraction skipped: scheduler payload detected")
        return MemoryExtractionResult()
    guard = privacy_guard or PrivacyGuard()
    decision = guard.should_store(text)
    if decision.action == "reject":
        return MemoryExtractionResult()
    content = decision.text.strip()
    llm_result = _extract_with_llm(content, assistant_output, config, recent_turns) if config else None
    if llm_result is not None:
        return llm_result
    return _extract_with_rules(content, guard)


# 传给抽取 LLM 的助手回复最多保留的字符数。抽取只关心用户说了什么,助手回复只作参考。
_MAX_ASSISTANT_CHARS = 1500


def _extract_with_llm(
    user_message: str,
    assistant_output: str,
    config: AppConfig | None,
    recent_turns: list[dict[str, str]] | None = None,
) -> MemoryExtractionResult | None:
    """调用抽取 LLM。返回 None 表示 LLM 不可用或调用失败(由调用方退回规则抽取)。"""
    started = time.perf_counter()
    payload: dict[str, Any] = {
        "user_message": user_message,
        "assistant_output": assistant_output[:_MAX_ASSISTANT_CHARS],
    }
    if recent_turns:
        payload["recent_turns"] = recent_turns
    try:
        output = run_memory_extraction(config, system_prompt=_llm_extraction_prompt(), payload=payload)
    except Exception as exc:
        logger.warning("memory llm extraction degraded: %s", exc)
        return None
    if output is None:
        return None
    memories = [memory for memory in (_extracted_from_json(item.model_dump()) for item in output.memories) if memory is not None]
    if output.memories and not memories:
        # 给了条目但没有一条能用(真实模型出现过 content 为空、只堆 tags 的输出):按失败处理,
        # 退回规则抽取。这和模型正常返回空列表("这轮没什么值得记")不是一回事。
        logger.warning("memory llm extraction returned %d unusable items, falling back to rules", len(output.memories))
        return None
    logger.info("memory llm extraction succeeded memories=%d duration_ms=%d", len(memories), int((time.perf_counter() - started) * 1000))
    return MemoryExtractionResult(memories=memories)


def _extract_with_rules(content: str, guard: PrivacyGuard) -> MemoryExtractionResult:
    """规则抽取兜底:仅处理明确偏好/约束,且强制原子化拆分。

    规则抽取不再是主路径(LLM 才是),只作为 LLM 失败时的降级方案。
    拆分策略:按标点/连接词把整段切成子句,每个子句独立判断类型。
    """
    lowered = content.lower()
    # 原子化拆分:按逗号/句号/分号/换行/连接词切分
    clauses = _split_into_clauses(content)
    if not clauses:
        return MemoryExtractionResult()

    memories: list[ExtractedMemory] = []
    stability = MemoryStability.STABLE if _contains_any(content, lowered, STABLE_MARKERS) else MemoryStability.SITUATIONAL
    if _contains_any(content, lowered, TEMPORARY_MARKERS):
        stability = MemoryStability.EPHEMERAL
    valid_until, decay_at = default_expiry(stability)

    for clause in clauses:
        clause = clause.strip()
        if len(clause) < 4:  # 太短无意义
            continue
        clause_lowered = clause.lower()
        memory_type = _memory_type(clause, clause_lowered)
        if memory_type is None:
            continue
        extracted = ExtractedMemory(
            memory_type=memory_type,
            content=clause,
            subject="user",
            predicate="states",
            object=clause,
            keywords=_keywords(clause),
            tags=_tags_for(clause, clause_lowered, memory_type),
            confidence=0.74,  # 规则抽取置信度低于 LLM
            stability=stability,
            sensitivity=guard.should_store(clause).sensitivity,
            source_quote=clause[:500],
        )
        extracted.valid_until = valid_until
        extracted.decay_at = decay_at
        memories.append(extracted)

    return MemoryExtractionResult(memories=memories)


_MEMORY_PREFIX_PATTERN = re.compile(
    r"^(?:(?:请你|请|你)?(?:记住|记得)|(?:please\s+)?remember(?:\s+that)?)[:：\s]*",
    re.IGNORECASE,
)


def _split_into_clauses(text: str) -> list[str]:
    """把整段文本按标点/连接词拆成原子化子句。

    示例:
        "记住：以后用中文回答,要简洁,格式用 markdown,不要废话"
        -> ["以后用中文回答", "要简洁", "格式用 markdown", "不要废话"]
    """
    # 按常见分隔符切分
    parts = re.split(r'[,，;；。\n]|(?:并且)|(?:同时)|(?:以及)|(?:还有)', text)
    # 每个子句都去掉"(请你)记住 / remember:"这类前缀:否则"好的,请你记住,以后……"
    # 里单独的"请你记住"会因含"记住"被当成一条偏好存下来。
    clauses = [_MEMORY_PREFIX_PATTERN.sub("", part.strip()).strip() for part in parts]
    return [clause for clause in clauses if clause]


def _memory_type(text: str, lowered: str) -> str | None:
    """判断子句的记忆类型(新类型体系)。"""
    if _contains_any(text, lowered, CONSTRAINT_MARKERS):
        return "user_constraint"
    if _contains_any(text, lowered, PREFERENCE_MARKERS) or _contains_any(text, lowered, IMPLICIT_PREFERENCE_MARKERS):
        return "user_preference"
    # 项目事实/决策不再由规则抽取处理——需要上下文理解,交给 LLM
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
    return load_system_prompt("memory/extraction")


def _extracted_from_json(item: dict[str, Any]) -> ExtractedMemory | None:
    raw_type = str(item.get("memory_type", "")).strip()
    # 旧类型名映射到新类型体系;不认识的类型原样保留(与旧行为一致)
    memory_type = normalize_memory_type(raw_type) or raw_type
    if not memory_type:
        return None
    content = str(item.get("content", "")).strip()
    if not content:
        return None
    stability_value = str(item.get("stability", "situational"))
    try:
        stability = MemoryStability(stability_value)
    except ValueError:
        stability = MemoryStability.SITUATIONAL
    confidence = float(item.get("confidence", 0.7))
    # LLM 标的 stability 要换成具体的过期 / 衰减时间,否则"临时""情境性"记忆永远不会过期。
    valid_until, decay_at = default_expiry(stability)
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
        valid_until=valid_until,
        decay_at=decay_at,
    )
