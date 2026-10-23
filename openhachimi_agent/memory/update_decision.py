"""把新抽取的记忆写入记忆库:先让 LLM 对照相近的旧记忆决定 ADD / UPDATE / DELETE / NONE。

做法参考 mem0。只看向量相似度分不清"换个说法"和"改了主意",也分不清"同一件事"和
"用词相近的两件事";交给 LLM 判断更可靠。LLM 不可用或调用失败时,退回
``conflicts.resolve_atom_conflict`` 的向量规则(相似时以新为准)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.conflicts import archive_atom, resolve_atom_conflict
from openhachimi_agent.memory.embeddings import EmbeddingProvider
from openhachimi_agent.memory.llm import MemoryUpdateDecision, run_memory_update
from openhachimi_agent.memory.models import ExtractedMemory, MemoryAtom, MemoryScope
from openhachimi_agent.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# 每条新记忆最多带给 LLM 的相近旧记忆数(全文检索 / 向量各取这么多,合并去重)。
_CANDIDATES_PER_MEMORY = 3
_VALID_EVENTS = {"ADD", "UPDATE", "DELETE", "NONE"}


@dataclass
class _PendingAtom:
    atom: MemoryAtom
    vector: list[float] | None


@dataclass
class ApplyResult:
    created: int = 0
    archived: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.created or self.archived)


def apply_extracted_memories(
    store: MemoryStore,
    config: AppConfig | None,
    scope: MemoryScope,
    turn_id: str,
    memories: list[ExtractedMemory],
    *,
    embedding_provider: EmbeddingProvider | None = None,
) -> ApplyResult:
    """把一轮抽取出的记忆写入记忆库,返回新增 / 归档条数。"""
    provider = _embedding_provider(config, embedding_provider)
    pending = [_PendingAtom(atom, _embed(provider, atom.content)) for atom in (_to_atom(m, scope, turn_id) for m in memories)]
    # 内容完全相同的旧记忆已存在:直接跳过,不必问 LLM
    pending = [item for item in pending if resolve_atom_conflict(store, item.atom).action != "dedupe"]
    if not pending:
        return ApplyResult()
    candidates = _find_candidates(store, config, scope, pending)
    decisions = _decide_with_llm(config, pending, candidates) if candidates else {}
    result = ApplyResult()
    for index, item in enumerate(pending):
        decision = decisions.get(index) if decisions is not None else None
        if decision is None:
            # 没有相近旧记忆(直接新增),或 LLM 不可用 / 漏判了这一条(退回向量规则)
            _apply_fallback(store, config, item, result, has_candidates=bool(candidates))
        else:
            _apply_decision(store, config, item, decision, candidates, result)
    return result


def _to_atom(extracted: ExtractedMemory, scope: MemoryScope, turn_id: str) -> MemoryAtom:
    return MemoryAtom(
        memory_type=extracted.memory_type,
        content=extracted.content,
        scope=scope,
        subject=extracted.subject,
        predicate=extracted.predicate,
        object=extracted.object,
        evidence_turn_ids=[turn_id],
        source_quote=extracted.source_quote,
        entities=extracted.entities,
        keywords=extracted.keywords,
        tags=extracted.tags,
        confidence=extracted.confidence,
        stability=extracted.stability,
        sensitivity=extracted.sensitivity,
        valid_until=extracted.valid_until,
        decay_at=extracted.decay_at,
    )


def _embedding_provider(config: AppConfig | None, provider: EmbeddingProvider | None) -> EmbeddingProvider | None:
    if not config or not config.memory.embedding.enabled:
        return None
    return provider or EmbeddingProvider(config.memory.embedding)


def _embed(provider: EmbeddingProvider | None, text: str) -> list[float] | None:
    if provider is None:
        return None
    embedding = provider.embed_sync(text)
    return None if embedding.degraded else embedding.vector


def _find_candidates(
    store: MemoryStore, config: AppConfig | None, scope: MemoryScope, pending: list[_PendingAtom]
) -> dict[str, dict[str, str]]:
    """为每条新记忆找相近的 active 旧记忆(L1),按 id 合并成一个列表。"""
    candidates: dict[str, dict[str, str]] = {}
    model = config.memory.embedding.model if config else None
    for item in pending:
        hits = [
            hit for hit in store.search(scope, item.atom.content, limit=_CANDIDATES_PER_MEMORY, touch_results=False)
            if hit.level == "L1" and hit.source != "recent"
        ]
        if item.vector:
            hits += store.vector_search(scope, item.vector, limit=_CANDIDATES_PER_MEMORY, model=model, levels=("L1",))
        for hit in hits:
            candidates.setdefault(hit.id, {"id": hit.id, "type": hit.memory_type, "content": hit.content})
    return candidates


def _decide_with_llm(
    config: AppConfig | None, pending: list[_PendingAtom], candidates: dict[str, dict[str, str]]
) -> dict[int, MemoryUpdateDecision] | None:
    """返回 {新记忆下标: 决定};LLM 不可用或失败时返回 None。"""
    payload: dict[str, Any] = {
        "existing": list(candidates.values()),
        "new": [{"index": index, "type": item.atom.memory_type, "content": item.atom.content} for index, item in enumerate(pending)],
    }
    try:
        output = run_memory_update(config, system_prompt=load_system_prompt("memory/update"), payload=payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory update decision degraded: %s", exc)
        return None
    if output is None:
        return None
    decisions: dict[int, MemoryUpdateDecision] = {}
    for decision in output.decisions:
        decision.event = decision.event.strip().upper()
        if decision.event in _VALID_EVENTS and 0 <= decision.index < len(pending):
            decisions.setdefault(decision.index, decision)
    return decisions


def _resolve_target_id(raw: str, candidates: dict[str, dict[str, str]]) -> str | None:
    """取出 LLM 指定的旧记忆 id。较弱的模型会往 target_id 里混进解释文字,这时找其中包含的候选 id。"""
    raw = (raw or "").strip()
    if raw in candidates:
        return raw
    contained = [candidate_id for candidate_id in candidates if candidate_id in raw]
    return contained[0] if len(contained) == 1 else None


def _apply_decision(
    store: MemoryStore,
    config: AppConfig | None,
    item: _PendingAtom,
    decision: MemoryUpdateDecision,
    candidates: dict[str, dict[str, str]],
    result: ApplyResult,
) -> None:
    target_id = _resolve_target_id(decision.target_id, candidates)
    event = decision.event
    # 目标 id 不在候选里(LLM 编造或写错):UPDATE 当作新增,DELETE 不执行
    if event == "UPDATE" and target_id is None:
        event = "ADD"
    if event == "DELETE" and target_id is None:
        event = "NONE"
    logger.info("memory update decision event=%s target=%s content=%r", event, target_id or "-", item.atom.content[:80])
    if event == "ADD":
        # LLM 说新增时仍过一遍向量规则兜底:真实模型会把"改用英文"这种改主意判成 ADD,
        # 结果新旧两条矛盾的偏好同时生效;同一槽位高度相似时以新为准替换旧的。
        _apply_fallback(store, config, item, result, has_candidates=True)
        return
    if event in {"UPDATE", "DELETE"}:
        archive_atom(store, target_id)
        result.archived += 1
    if event == "UPDATE" and decision.content.strip() and decision.content.strip() != item.atom.content:
        item.atom.content = decision.content.strip()
        item.atom.object = item.atom.content
        item.vector = None  # 内容变了,原向量作废,交给后台重新算
    if event == "UPDATE":
        _insert(store, config, item)
        result.created += 1


def _apply_fallback(
    store: MemoryStore, config: AppConfig | None, item: _PendingAtom, result: ApplyResult, *, has_candidates: bool
) -> None:
    if has_candidates:
        conflict = resolve_atom_conflict(
            store,
            item.atom,
            embedding_vector=item.vector,
            embedding_model=config.memory.embedding.model if config and item.vector else None,
        )
        if conflict.action == "dedupe":
            return
        if conflict.action == "supersede":
            result.archived += 1
    _insert(store, config, item)
    result.created += 1


def _insert(store: MemoryStore, config: AppConfig | None, item: _PendingAtom) -> None:
    atom_id = store.add_atom(item.atom)
    if not config or not config.memory.embedding.enabled:
        return
    model = config.memory.embedding.model
    if item.vector:
        store.save_vector(atom_id, "L1", model, item.vector)
    else:
        store.enqueue_unique_job(
            "embed_memory_item",
            {"item_id": atom_id, "level": "L1", "text": item.atom.content, "model": model},
            dedupe_key=f"embed:L1:{atom_id}",
        )


__all__ = ["ApplyResult", "apply_extracted_memories"]
