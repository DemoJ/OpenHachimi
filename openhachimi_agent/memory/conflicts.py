"""长期记忆冲突处理。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from openhachimi_agent.memory.models import MemoryAtom, MemoryStatus


@dataclass(frozen=True)
class ConflictDecision:
    action: Literal["insert", "dedupe", "supersede"]
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
    """解决记忆冲突。

    冲突解决策略:
    1. 完全同内容: dedupe(保留旧者)
    2. 向量相似但内容不同: 比较置信度,新者置信度显著更高则 supersede(新者替换旧者),
       否则 dedupe(保留旧者)
    3. 无冲突: insert
    """
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
            old_confidence = float(row["confidence"])
            new_confidence = atom.confidence
            # 新记忆置信度显著更高(差值>0.15)且语义近似: supersede,新者替换旧者
            # 例如用户先说"用Python2",后说"用Python3",后者应替换前者
            if new_confidence > old_confidence + 0.15:
                # 归档旧记忆
                _archive_atom(store, row["id"])
                return ConflictDecision(
                    action="supersede",
                    winner_id=atom.id,
                    loser_id=row["id"],
                    reason=f"supersede_higher_confidence:{similarity:.4f}:{old_confidence:.2f}->{new_confidence:.2f}",
                    conflict_key=conflict_key,
                )
            # 置信度相近或旧者更高: dedupe,保留旧者
            return ConflictDecision(
                action="dedupe",
                winner_id=row["id"],
                loser_id=atom.id,
                reason=f"vector_similar:{similarity:.4f}",
                conflict_key=conflict_key,
            )
    return ConflictDecision(action="insert", winner_id=atom.id, reason="no_conflict", conflict_key=conflict_key)


def _archive_atom(store, atom_id: str) -> None:
    """归档被 supersede 的旧记忆。"""
    from openhachimi_agent.memory.models import utc_now_iso
    now = utc_now_iso()
    with store.connect() as conn:
        conn.execute(
            "UPDATE memory_atoms SET status = ?, updated_at = ? WHERE id = ?",
            (MemoryStatus.ARCHIVED.value, now, atom_id),
        )
        conn.execute("DELETE FROM memory_atoms_fts WHERE id = ?", (atom_id,))
