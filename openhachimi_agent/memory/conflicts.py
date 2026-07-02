"""长期记忆冲突处理。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from openhachimi_agent.memory.models import MemoryAtom


@dataclass(frozen=True)
class ConflictDecision:
    action: Literal["insert", "dedupe"]
    winner_id: str | None = None
    loser_id: str | None = None
    reason: str = ""
    conflict_key: str = ""


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def normalized_memory_key(atom: MemoryAtom) -> tuple[str, str, str]:
    return (atom.memory_type, atom.subject.lower(), atom.predicate.lower())


def conflict_key_for_atom(atom: MemoryAtom) -> str:
    return "|".join(normalized_memory_key(atom))


def resolve_atom_conflict(
    store,
    atom: MemoryAtom,
    *,
    embedding_vector: list[float] | None = None,
    embedding_model: str | None = None,
    similarity_threshold: float = 0.82,
) -> ConflictDecision:
    conflict_key = conflict_key_for_atom(atom)
    normalized_content = _normalize_text(atom.normalized_content or atom.content)
    for row in store.find_conflict_candidates(atom.scope, atom):
        old_content = _normalize_text(row["normalized_content"] or row["content"])
        if old_content == normalized_content:
            return ConflictDecision(
                action="dedupe",
                winner_id=row["id"],
                loser_id=atom.id,
                reason="same_normalized_content",
                conflict_key=conflict_key,
            )
    if embedding_vector:
        similar = store.find_similar_atom_by_vector(
            atom.scope,
            atom,
            embedding_vector,
            model=embedding_model,
            threshold=similarity_threshold,
        )
        if similar:
            row, similarity = similar
            # 阈值内统一去重(保留旧者,丢弃新者):语义近似更可能是同一事实的
            # 不同措辞而非偏好演进,保留旧者避免堆积,也避免误覆盖用户后续细化陈述。
            return ConflictDecision(
                action="dedupe",
                winner_id=row["id"],
                loser_id=atom.id,
                reason=f"vector_similar:{similarity:.4f}",
                conflict_key=conflict_key,
            )
    return ConflictDecision(action="insert", winner_id=atom.id, reason="no_conflict", conflict_key=conflict_key)
