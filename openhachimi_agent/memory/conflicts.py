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


# 相似记忆冲突时,新记忆置信度低于这个值(抽取器自己都没把握)才保留旧记忆,否则以新为准。
# 不按"比旧记忆低多少"比较:真实对话里"改用英文"的新记忆置信度 0.7、旧偏好 0.9,
# 按相对差值会把用户刚改的主意直接丢掉。
_MIN_SUPERSEDE_CONFIDENCE = 0.6


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
    2. 同一槽位(主语 + 谓语)向量相似但内容不同: 默认 supersede,新说法替换旧说法;
       只有新记忆置信度本身很低(低于 0.6)时才 dedupe 保留旧者。
       只看向量分不清"换个说法"和"改了主意"(如"用 Python2"→"用 Python3"):
       前者用新说法替换不丢信息,后者必须以新为准;旧实现要求新者置信度高 0.15
       才替换,而大多数记忆置信度都在 0.7–0.9,结果几乎总是留着旧偏好。
    3. 无冲突: insert
    """
    conflict_key = conflict_key_for_atom(atom)
    exact = _exact_duplicate(store, atom, conflict_key)
    if exact is not None:
        return exact
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
            return _resolve_similar(store, atom, row, similarity, conflict_key)
    return ConflictDecision(action="insert", winner_id=atom.id, reason="no_conflict", conflict_key=conflict_key)


def _exact_duplicate(store, atom: MemoryAtom, conflict_key: str) -> ConflictDecision | None:
    """同一槽位已有内容完全相同(忽略大小写和空白)的记忆:dedupe,保留旧者。"""
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
    return None


def _resolve_similar(store, atom: MemoryAtom, row, similarity: float, conflict_key: str) -> ConflictDecision:
    """向量相似但内容不同:默认以新为准(归档旧者);新记忆本身置信度很低时保留旧者。"""
    old_confidence = float(row["confidence"])
    new_confidence = atom.confidence
    if new_confidence < _MIN_SUPERSEDE_CONFIDENCE:
        return ConflictDecision(
            action="dedupe",
            winner_id=row["id"],
            loser_id=atom.id,
            reason=f"vector_similar_keep_old:{similarity:.4f}:{old_confidence:.2f}>{new_confidence:.2f}",
            conflict_key=conflict_key,
        )
    archive_atom(store, row["id"])
    return ConflictDecision(
        action="supersede",
        winner_id=atom.id,
        loser_id=row["id"],
        reason=f"supersede_newer:{similarity:.4f}:{old_confidence:.2f}->{new_confidence:.2f}",
        conflict_key=conflict_key,
    )


def archive_atom(store, atom_id: str) -> None:
    """归档被新记忆替换或判定失效的旧记忆。"""
    from openhachimi_agent.memory.models import utc_now_iso
    now = utc_now_iso()
    with store.connect() as conn:
        conn.execute(
            "UPDATE memory_atoms SET status = ?, updated_at = ? WHERE id = ?",
            (MemoryStatus.ARCHIVED.value, now, atom_id),
        )
        conn.execute("DELETE FROM memory_atoms_fts WHERE id = ?", (atom_id,))


# 旧名字,保留兼容
_archive_atom = archive_atom
