"""长期记忆冲突处理。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from openhachimi_agent.memory.models import MemoryAtom


@dataclass(frozen=True)
class ConflictDecision:
    action: Literal["insert", "dedupe", "supersede", "keep_both"]
    winner_id: str | None = None
    loser_id: str | None = None
    reason: str = ""
    conflict_key: str = ""
    conflict_group_id: str | None = None


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
    similarity_threshold: float = 0.92,
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
            if _normalize_text(row["content"]) == normalized_content:
                return ConflictDecision(
                    action="dedupe",
                    winner_id=row["id"],
                    loser_id=atom.id,
                    reason=f"vector_duplicate:{similarity:.4f}",
                    conflict_key=conflict_key,
                )
            group_id = atom.conflict_group_id or uuid4().hex
            atom.supersedes_id = row["id"]
            atom.conflict_group_id = group_id
            return ConflictDecision(
                action="supersede",
                winner_id=atom.id,
                loser_id=row["id"],
                reason=f"vector_similarity:{similarity:.4f}",
                conflict_key=conflict_key,
                conflict_group_id=group_id,
            )
    return ConflictDecision(action="insert", winner_id=atom.id, reason="no_conflict", conflict_key=conflict_key)
