from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

from openhachimi_agent.memory.models import (
    MemoryAtom,
    MemoryScope,
    MemorySensitivity,
    MemoryStatus,
    utc_now_iso,
)
from openhachimi_agent.memory._store.utils import (
    _json,
    _load_json_array,
)
from openhachimi_agent.memory.vector_index import cosine_similarity


class AtomStoreMixin:
    def add_atom(self, atom: MemoryAtom) -> str:
        normalized = atom.normalized_content or atom.content.strip().lower()
        keywords_text = " ".join(atom.keywords)
        search_text = atom.search_text or " ".join([atom.content, keywords_text, " ".join(atom.entities), " ".join(atom.tags)])
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_atoms(
                    id, tenant_id, user_id, role_name, session_id, channel, memory_type,
                    subject, predicate, object, content, normalized_content, search_text,
                    evidence_turn_ids_json, source_quote, entities_json, keywords_json,
                    tags_json, scope_json, confidence, stability, sensitivity, valid_from,
                    valid_until, decay_at, status, supersedes_id, superseded_by_id,
                    conflict_group_id, embedding_status, created_at, updated_at,
                    last_accessed_at, access_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    user_id = excluded.user_id,
                    role_name = excluded.role_name,
                    session_id = excluded.session_id,
                    channel = excluded.channel,
                    memory_type = excluded.memory_type,
                    subject = excluded.subject,
                    predicate = excluded.predicate,
                    object = excluded.object,
                    content = excluded.content,
                    normalized_content = excluded.normalized_content,
                    search_text = excluded.search_text,
                    evidence_turn_ids_json = CASE
                        WHEN excluded.evidence_turn_ids_json = '[]' THEN memory_atoms.evidence_turn_ids_json
                        ELSE excluded.evidence_turn_ids_json
                    END,
                    source_quote = COALESCE(NULLIF(excluded.source_quote, ''), memory_atoms.source_quote),
                    entities_json = excluded.entities_json,
                    keywords_json = excluded.keywords_json,
                    tags_json = excluded.tags_json,
                    scope_json = excluded.scope_json,
                    confidence = excluded.confidence,
                    stability = excluded.stability,
                    sensitivity = excluded.sensitivity,
                    valid_from = excluded.valid_from,
                    valid_until = excluded.valid_until,
                    decay_at = excluded.decay_at,
                    status = excluded.status,
                    supersedes_id = COALESCE(excluded.supersedes_id, memory_atoms.supersedes_id),
                    superseded_by_id = COALESCE(excluded.superseded_by_id, memory_atoms.superseded_by_id),
                    conflict_group_id = COALESCE(excluded.conflict_group_id, memory_atoms.conflict_group_id),
                    embedding_status = excluded.embedding_status,
                    updated_at = excluded.updated_at
                """,
                (
                    atom.id,
                    atom.scope.tenant_id,
                    atom.scope.user_id,
                    atom.scope.role_name,
                    atom.scope.session_id,
                    atom.scope.channel,
                    atom.memory_type,
                    atom.subject,
                    atom.predicate,
                    atom.object,
                    atom.content,
                    normalized,
                    search_text,
                    _json(atom.evidence_turn_ids),
                    atom.source_quote,
                    _json(atom.entities),
                    _json(atom.keywords),
                    _json(atom.tags),
                    _json(atom.scope.to_json_dict()),
                    atom.confidence,
                    str(atom.stability),
                    str(atom.sensitivity),
                    atom.valid_from,
                    atom.valid_until,
                    atom.decay_at,
                    str(atom.status),
                    atom.supersedes_id,
                    atom.superseded_by_id,
                    atom.conflict_group_id,
                    atom.embedding_status,
                    atom.created_at,
                    atom.updated_at,
                    atom.last_accessed_at,
                    atom.access_count,
                ),
            )
            conn.execute("DELETE FROM memory_atoms_fts WHERE id = ?", (atom.id,))
            if str(atom.status) == MemoryStatus.ACTIVE.value:
                conn.execute(
                    "INSERT INTO memory_atoms_fts(id, content, search_text, keywords) VALUES(?,?,?,?)",
                    (atom.id, atom.content, search_text, keywords_text),
                )
        return atom.id

    def update_atom_content(self, atom_id: str, content: str, *, embedding_status: str = "pending") -> bool:
        normalized = content.strip().lower()
        search_text = content
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT status, keywords_json FROM memory_atoms WHERE id = ?", (atom_id,)).fetchone()
            if row is None:
                return False
            keywords_text = " ".join(_load_json_array(row["keywords_json"]))
            search_text = " ".join([content, keywords_text]) if keywords_text else content
            conn.execute(
                """
                UPDATE memory_atoms
                SET content = ?, object = ?, normalized_content = ?, search_text = ?, embedding_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (content, content, normalized, search_text, embedding_status, now, atom_id),
            )
            conn.execute("DELETE FROM memory_atoms_fts WHERE id = ?", (atom_id,))
            if row["status"] == MemoryStatus.ACTIVE.value:
                conn.execute(
                    "INSERT INTO memory_atoms_fts(id, content, search_text, keywords) VALUES(?,?,?,?)",
                    (atom_id, content, search_text, keywords_text),
                )
            return True

    def set_atom_embedding_status(self, atom_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE memory_atoms SET embedding_status = ?, updated_at = ? WHERE id = ?", (status, utc_now_iso(), atom_id))

    def get_atom_content(self, atom_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT content FROM memory_atoms WHERE id = ?", (atom_id,)).fetchone()
            return str(row["content"]) if row else None

    def find_similar_atom_by_vector(
        self,
        scope: MemoryScope,
        atom: MemoryAtom,
        query_vector: list[float],
        *,
        model: str | None = None,
        threshold: float = 0.92,
        limit: int = 50,
    ) -> tuple[sqlite3.Row, float] | None:
        if not query_vector:
            return None
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*, v.vector_json
                FROM memory_vectors v
                JOIN memory_atoms a ON a.id = v.item_id
                WHERE v.level = 'L1'
                  AND (? IS NULL OR v.model = ?)
                  AND a.status = ?
                  AND a.tenant_id = ?
                  AND a.user_id = ?
                  AND (a.role_name = ? OR a.role_name = '')
                  AND a.memory_type = ?
                  AND lower(a.subject) = lower(?)
                  AND lower(a.predicate) = lower(?)
                  AND a.id != ?
                  AND a.sensitivity != ?
                ORDER BY a.updated_at DESC
                LIMIT ?
                """,
                (
                    model,
                    model,
                    MemoryStatus.ACTIVE.value,
                    scope.tenant_id,
                    scope.user_id,
                    scope.role_name,
                    atom.memory_type,
                    atom.subject,
                    atom.predicate,
                    atom.id,
                    MemorySensitivity.SECRET.value,
                    limit,
                ),
            ).fetchall()
        best: tuple[sqlite3.Row, float] | None = None
        for row in rows:
            try:
                vector = [float(value) for value in json.loads(row["vector_json"])]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            similarity = cosine_similarity(query_vector, vector)
            if similarity >= threshold and (best is None or similarity > best[1]):
                best = (row, similarity)
        return best

    def find_conflict_candidates(self, scope: MemoryScope, atom: MemoryAtom, limit: int = 50) -> list[sqlite3.Row]:
        normalized = (atom.normalized_content or atom.content).strip().lower()
        object_value = (atom.object or "").strip().lower()
        with self.connect() as conn:
            exact_rows = conn.execute(
                """
                SELECT * FROM memory_atoms
                WHERE status = ? AND tenant_id = ? AND user_id = ? AND (role_name = ? OR role_name = '')
                  AND memory_type = ? AND lower(subject) = lower(?) AND lower(predicate) = lower(?) AND id != ?
                  AND (lower(normalized_content) = ? OR (? != '' AND lower(object) = ?))
                ORDER BY updated_at DESC
                """,
                (
                    MemoryStatus.ACTIVE.value,
                    scope.tenant_id,
                    scope.user_id,
                    scope.role_name,
                    atom.memory_type,
                    atom.subject,
                    atom.predicate,
                    atom.id,
                    normalized,
                    object_value,
                    object_value,
                ),
            ).fetchall()
            recent_rows = conn.execute(
                """
                SELECT * FROM memory_atoms
                WHERE status = ? AND tenant_id = ? AND user_id = ? AND (role_name = ? OR role_name = '')
                  AND memory_type = ? AND lower(subject) = lower(?) AND lower(predicate) = lower(?) AND id != ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (
                    MemoryStatus.ACTIVE.value,
                    scope.tenant_id,
                    scope.user_id,
                    scope.role_name,
                    atom.memory_type,
                    atom.subject,
                    atom.predicate,
                    atom.id,
                    limit,
                ),
            ).fetchall()
        rows_by_id: dict[str, sqlite3.Row] = {}
        for row in [*exact_rows, *recent_rows]:
            rows_by_id[str(row["id"])] = row
        return list(rows_by_id.values())

    def mark_atom_superseded(self, old_id: str, new_id: str, conflict_group_id: str | None = None) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE memory_atoms SET status = ?, superseded_by_id = ?, conflict_group_id = ?, updated_at = ? WHERE id = ?",
                (MemoryStatus.SUPERSEDED.value, new_id, conflict_group_id, now, old_id),
            )
            conn.execute("DELETE FROM memory_atoms_fts WHERE id = ?", (old_id,))
            conn.execute(
                "UPDATE memory_atoms SET supersedes_id = COALESCE(supersedes_id, ?), conflict_group_id = COALESCE(conflict_group_id, ?), updated_at = ? WHERE id = ?",
                (old_id, conflict_group_id, now, new_id),
            )

    def record_conflict(self, scope: MemoryScope, conflict_key: str, winner_id: str, loser_id: str, reason: str) -> str:
        conflict_id = uuid4().hex
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_conflicts(
                    id, tenant_id, user_id, role_name, conflict_key, winner_id, loser_id, status, reason, created_at, resolved_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (conflict_id, scope.tenant_id, scope.user_id, scope.role_name, conflict_key, winner_id, loser_id, "resolved", reason, now, now),
            )
        return conflict_id

    def expire_due_atoms(self, *, now: str | None = None, limit: int = 500) -> int:
        current = now or utc_now_iso()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM memory_atoms WHERE status = ? AND valid_until IS NOT NULL AND valid_until <= ? LIMIT ?",
                (MemoryStatus.ACTIVE.value, current, limit),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            conn.executemany(
                "UPDATE memory_atoms SET status = ?, updated_at = ? WHERE id = ?",
                [(MemoryStatus.EXPIRED.value, current, atom_id) for atom_id in ids],
            )
            conn.executemany("DELETE FROM memory_atoms_fts WHERE id = ?", [(atom_id,) for atom_id in ids])
            return len(ids)
