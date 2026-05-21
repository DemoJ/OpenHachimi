"""长期记忆 SQLite 存储。"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from openhachimi_agent.memory.models import (
    MemoryAtom,
    MemoryBlock,
    MemoryJob,
    MemoryJobStatus,
    MemoryProfile,
    MemoryScope,
    MemorySearchResult,
    MemorySensitivity,
    MemoryStatus,
    MemoryTurn,
    utc_now_iso,
)

SCHEMA_VERSION = 1


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json_array(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_turns (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    assistant_output TEXT NOT NULL,
                    tool_calls_summary_json TEXT NOT NULL,
                    task_frame_json TEXT NOT NULL,
                    memory_context_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_summary TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    raw_messages_json_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_atoms (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    evidence_turn_ids_json TEXT NOT NULL,
                    source_quote TEXT NOT NULL,
                    entities_json TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    stability TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    valid_from TEXT,
                    valid_until TEXT,
                    decay_at TEXT,
                    status TEXT NOT NULL,
                    supersedes_id TEXT,
                    superseded_by_id TEXT,
                    conflict_group_id TEXT,
                    embedding_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_atoms_fts USING fts5(
                    id UNINDEXED,
                    content,
                    search_text,
                    keywords
                );

                CREATE TABLE IF NOT EXISTS memory_blocks (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    block_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    atom_ids_json TEXT NOT NULL,
                    entities_json TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    coherence_score REAL NOT NULL,
                    freshness_score REAL NOT NULL,
                    status TEXT NOT NULL,
                    last_consolidated_at TEXT,
                    embedding_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_blocks_fts USING fts5(
                    id UNINDEXED,
                    title,
                    summary,
                    details,
                    search_text,
                    keywords
                );

                CREATE TABLE IF NOT EXISTS memory_profiles (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role_name TEXT,
                    profile_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    traits_json TEXT NOT NULL,
                    preferences_json TEXT NOT NULL,
                    constraints_json TEXT NOT NULL,
                    dislikes_json TEXT NOT NULL,
                    evidence_atom_ids_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    stability TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_reviewed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_profiles_fts USING fts5(
                    id UNINDEXED,
                    title,
                    summary
                );

                CREATE TABLE IF NOT EXISTS memory_vectors (
                    item_id TEXT PRIMARY KEY,
                    level TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_jobs (
                    id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    run_after TEXT NOT NULL,
                    locked_at TEXT,
                    last_error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_atoms_scope ON memory_atoms(tenant_id, user_id, role_name, status);
                CREATE INDEX IF NOT EXISTS idx_memory_blocks_scope ON memory_blocks(tenant_id, user_id, role_name, status);
                CREATE INDEX IF NOT EXISTS idx_memory_profiles_scope ON memory_profiles(tenant_id, user_id, role_name, status);
                CREATE INDEX IF NOT EXISTS idx_memory_vectors_level ON memory_vectors(level, model);
                CREATE INDEX IF NOT EXISTS idx_memory_jobs_status ON memory_jobs(status, run_after);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO memory_schema_migrations(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, utc_now_iso()),
            )

    def add_turn(self, turn: MemoryTurn) -> str:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_turns(
                    id, turn_id, tenant_id, user_id, role_name, session_id, channel,
                    user_message, assistant_output, tool_calls_summary_json, task_frame_json,
                    memory_context_ids_json, status, error_summary, started_at, finished_at,
                    duration_ms, raw_messages_json_ref, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    turn.id,
                    turn.turn_id,
                    turn.tenant_id,
                    turn.user_id,
                    turn.role_name,
                    turn.session_id,
                    turn.channel,
                    turn.user_message,
                    turn.assistant_output,
                    turn.tool_calls_summary_json,
                    turn.task_frame_json,
                    turn.memory_context_ids_json,
                    turn.status,
                    turn.error_summary,
                    turn.started_at,
                    turn.finished_at,
                    turn.duration_ms,
                    turn.raw_messages_json_ref,
                    turn.created_at,
                ),
            )
        return turn.id

    def add_atom(self, atom: MemoryAtom) -> str:
        normalized = atom.normalized_content or atom.content.strip().lower()
        keywords_text = " ".join(atom.keywords)
        search_text = atom.search_text or " ".join([atom.content, keywords_text, " ".join(atom.entities), " ".join(atom.tags)])
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_atoms(
                    id, tenant_id, user_id, role_name, session_id, channel, memory_type,
                    subject, predicate, object, content, normalized_content, search_text,
                    evidence_turn_ids_json, source_quote, entities_json, keywords_json,
                    tags_json, scope_json, confidence, stability, sensitivity, valid_from,
                    valid_until, decay_at, status, supersedes_id, superseded_by_id,
                    conflict_group_id, embedding_status, created_at, updated_at,
                    last_accessed_at, access_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            conn.execute(
                "INSERT INTO memory_atoms_fts(id, content, search_text, keywords) VALUES(?,?,?,?)",
                (atom.id, atom.content, search_text, keywords_text),
            )
        return atom.id

    def add_block(self, block: MemoryBlock) -> str:
        keywords_text = " ".join(block.keywords)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_blocks(
                    id, tenant_id, user_id, role_name, session_id, channel, block_type,
                    title, summary, details, search_text, atom_ids_json, entities_json,
                    keywords_json, tags_json, scope_json, confidence, coherence_score,
                    freshness_score, status, last_consolidated_at, embedding_status,
                    created_at, updated_at, last_accessed_at, access_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    block.id,
                    block.scope.tenant_id,
                    block.scope.user_id,
                    block.scope.role_name,
                    block.scope.session_id,
                    block.scope.channel,
                    block.block_type,
                    block.title,
                    block.summary,
                    block.details,
                    block.search_text or " ".join([block.title, block.summary, keywords_text]),
                    _json(block.atom_ids),
                    _json(block.entities),
                    _json(block.keywords),
                    _json(block.tags),
                    _json(block.scope.to_json_dict()),
                    block.confidence,
                    block.coherence_score,
                    block.freshness_score,
                    str(block.status),
                    block.last_consolidated_at,
                    block.embedding_status,
                    block.created_at,
                    block.updated_at,
                    block.last_accessed_at,
                    block.access_count,
                ),
            )
            conn.execute("DELETE FROM memory_blocks_fts WHERE id = ?", (block.id,))
            conn.execute(
                "INSERT INTO memory_blocks_fts(id, title, summary, details, search_text, keywords) VALUES(?,?,?,?,?,?)",
                (block.id, block.title, block.summary, block.details, block.search_text, keywords_text),
            )
        return block.id

    def add_profile(self, profile: MemoryProfile) -> str:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_profiles(
                    id, tenant_id, user_id, role_name, profile_type, title, summary,
                    traits_json, preferences_json, constraints_json, dislikes_json,
                    evidence_atom_ids_json, confidence, stability, status, last_reviewed_at,
                    created_at, updated_at, last_accessed_at, access_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    profile.id,
                    profile.tenant_id,
                    profile.user_id,
                    profile.role_name,
                    profile.profile_type,
                    profile.title,
                    profile.summary,
                    _json(profile.traits),
                    _json(profile.preferences),
                    _json(profile.constraints),
                    _json(profile.dislikes),
                    _json(profile.evidence_atom_ids),
                    profile.confidence,
                    str(profile.stability),
                    str(profile.status),
                    profile.last_reviewed_at,
                    profile.created_at,
                    profile.updated_at,
                    profile.last_accessed_at,
                    profile.access_count,
                ),
            )
            conn.execute("DELETE FROM memory_profiles_fts WHERE id = ?", (profile.id,))
            conn.execute(
                "INSERT INTO memory_profiles_fts(id, title, summary) VALUES(?,?,?)",
                (profile.id, profile.title, profile.summary),
            )
        return profile.id

    def enqueue_job(self, job: MemoryJob) -> str:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_jobs(
                    id, job_type, payload_json, status, attempts, max_attempts, run_after,
                    locked_at, last_error, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.id,
                    job.job_type,
                    _json(job.payload),
                    str(job.status),
                    job.attempts,
                    job.max_attempts,
                    job.run_after,
                    job.locked_at,
                    job.last_error,
                    job.created_at,
                    job.updated_at,
                ),
            )
        return job.id

    def set_atom_embedding_status(self, atom_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE memory_atoms SET embedding_status = ?, updated_at = ? WHERE id = ?", (status, utc_now_iso(), atom_id))

    def save_vector(self, item_id: str, level: str, model: str, vector: list[float]) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_vectors(item_id, level, model, dimensions, vector_json, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (item_id, level, model, len(vector), _json(vector), now, now),
            )
            if level == "L1":
                conn.execute("UPDATE memory_atoms SET embedding_status = ?, updated_at = ? WHERE id = ?", ("ready", now, item_id))
            elif level == "L2":
                conn.execute("UPDATE memory_blocks SET embedding_status = ?, updated_at = ? WHERE id = ?", ("ready", now, item_id))

    def get_atom_content(self, atom_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT content FROM memory_atoms WHERE id = ?", (atom_id,)).fetchone()
            return str(row["content"]) if row else None

    def vector_search(self, scope: MemoryScope, query_vector: list[float], limit: int = 10, model: str | None = None) -> list[MemorySearchResult]:
        if not query_vector:
            return []
        limit = max(1, min(limit, 50))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*, v.vector_json, v.model
                FROM memory_vectors v
                JOIN memory_atoms a ON a.id = v.item_id
                WHERE v.level = 'L1'
                  AND (? IS NULL OR v.model = ?)
                  AND a.status = ?
                  AND a.tenant_id = ?
                  AND a.user_id = ?
                  AND (a.role_name = ? OR a.role_name = '')
                  AND a.sensitivity != ?
                """,
                (model, model, MemoryStatus.ACTIVE.value, scope.tenant_id, scope.user_id, scope.role_name, MemorySensitivity.SECRET.value),
            ).fetchall()
        scored: list[MemorySearchResult] = []
        for row in rows:
            try:
                vector = [float(value) for value in json.loads(row["vector_json"])]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            similarity = self._cosine_similarity(query_vector, vector)
            if similarity <= 0:
                continue
            scored.append(
                MemorySearchResult(
                    id=row["id"],
                    level="L1",
                    content=row["content"],
                    score=similarity * float(row["confidence"]),
                    memory_type=row["memory_type"],
                    confidence=row["confidence"],
                    updated_at=row["updated_at"],
                    source="vector",
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]

    def search(self, scope: MemoryScope, query: str, limit: int = 10, include_archived: bool = False) -> list[MemorySearchResult]:
        limit = max(1, min(limit, 50))
        terms = self._query_terms(query)
        if not terms:
            return []
        fts_query = " OR ".join(terms)
        statuses = [MemoryStatus.ACTIVE.value]
        if include_archived:
            statuses.append(MemoryStatus.ARCHIVED.value)
        results: list[MemorySearchResult] = []
        with self.connect() as conn:
            results.extend(self._search_atoms(conn, scope, fts_query, statuses, limit))
            results.extend(self._search_blocks(conn, scope, fts_query, statuses, limit))
            results.extend(self._search_profiles(conn, scope, fts_query, statuses, limit))
            if not results:
                results.extend(self._search_like(conn, scope, terms, statuses, limit))
            if not results:
                results.extend(self._recent_atoms(conn, scope, statuses, limit))
            results.sort(key=lambda item: item.score, reverse=True)
            final = results[:limit]
            self.touch([item.id for item in final])
            return final

    def _search_atoms(self, conn: sqlite3.Connection, scope: MemoryScope, query: str, statuses: list[str], limit: int) -> list[MemorySearchResult]:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT a.*, bm25(memory_atoms_fts) AS rank
            FROM memory_atoms_fts
            JOIN memory_atoms a ON a.id = memory_atoms_fts.id
            WHERE memory_atoms_fts MATCH ?
              AND a.status IN ({placeholders})
              AND a.tenant_id = ?
              AND a.user_id = ?
              AND (a.role_name = ? OR a.role_name = '')
              AND a.sensitivity != ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, *statuses, scope.tenant_id, scope.user_id, scope.role_name, MemorySensitivity.SECRET.value, limit),
        ).fetchall()
        return [
            MemorySearchResult(
                id=row["id"],
                level="L1",
                content=row["content"],
                score=self._rank_to_score(row["rank"], row["confidence"]),
                memory_type=row["memory_type"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                source="bm25",
                metadata={"keywords": _load_json_array(row["keywords_json"])},
            )
            for row in rows
        ]

    def _search_blocks(self, conn: sqlite3.Connection, scope: MemoryScope, query: str, statuses: list[str], limit: int) -> list[MemorySearchResult]:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT b.*, bm25(memory_blocks_fts) AS rank
            FROM memory_blocks_fts
            JOIN memory_blocks b ON b.id = memory_blocks_fts.id
            WHERE memory_blocks_fts MATCH ?
              AND b.status IN ({placeholders})
              AND b.tenant_id = ?
              AND b.user_id = ?
              AND (b.role_name = ? OR b.role_name = '')
            ORDER BY rank
            LIMIT ?
            """,
            (query, *statuses, scope.tenant_id, scope.user_id, scope.role_name, limit),
        ).fetchall()
        return [
            MemorySearchResult(
                id=row["id"],
                level="L2",
                content=f"{row['title']}：{row['summary']}",
                score=self._rank_to_score(row["rank"], row["confidence"]) * 1.08,
                memory_type=row["block_type"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                source="bm25",
            )
            for row in rows
        ]

    def _search_profiles(self, conn: sqlite3.Connection, scope: MemoryScope, query: str, statuses: list[str], limit: int) -> list[MemorySearchResult]:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT p.*, bm25(memory_profiles_fts) AS rank
            FROM memory_profiles_fts
            JOIN memory_profiles p ON p.id = memory_profiles_fts.id
            WHERE memory_profiles_fts MATCH ?
              AND p.status IN ({placeholders})
              AND p.tenant_id = ?
              AND p.user_id = ?
              AND (p.role_name = ? OR p.role_name IS NULL)
            ORDER BY rank
            LIMIT ?
            """,
            (query, *statuses, scope.tenant_id, scope.user_id, scope.role_name, limit),
        ).fetchall()
        return [
            MemorySearchResult(
                id=row["id"],
                level="L3",
                content=f"{row['title']}：{row['summary']}",
                score=self._rank_to_score(row["rank"], row["confidence"]) * 1.15,
                memory_type=row["profile_type"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                source="bm25",
            )
            for row in rows
        ]

    def _search_like(
        self,
        conn: sqlite3.Connection,
        scope: MemoryScope,
        terms: list[str],
        statuses: list[str],
        limit: int,
    ) -> list[MemorySearchResult]:
        placeholders = ",".join("?" for _ in statuses)
        like_parts = " OR ".join("(content LIKE ? OR search_text LIKE ? OR keywords_json LIKE ?)" for _ in terms)
        like_args = [arg for term in terms for arg in (f"%{term}%", f"%{term}%", f"%{term}%")]
        rows = conn.execute(
            f"""
            SELECT * FROM memory_atoms
            WHERE status IN ({placeholders})
              AND tenant_id = ?
              AND user_id = ?
              AND (role_name = ? OR role_name = '')
              AND sensitivity != ?
              AND ({like_parts})
            LIMIT ?
            """,
            (*statuses, scope.tenant_id, scope.user_id, scope.role_name, MemorySensitivity.SECRET.value, *like_args, limit),
        ).fetchall()
        return [
            MemorySearchResult(
                id=row["id"],
                level="L1",
                content=row["content"],
                score=float(row["confidence"]),
                memory_type=row["memory_type"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                source="like",
            )
            for row in rows
        ]

    def _recent_atoms(
        self,
        conn: sqlite3.Connection,
        scope: MemoryScope,
        statuses: list[str],
        limit: int,
    ) -> list[MemorySearchResult]:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT * FROM memory_atoms
            WHERE status IN ({placeholders})
              AND tenant_id = ?
              AND user_id = ?
              AND (role_name = ? OR role_name = '')
              AND sensitivity != ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (*statuses, scope.tenant_id, scope.user_id, scope.role_name, MemorySensitivity.SECRET.value, limit),
        ).fetchall()
        return [
            MemorySearchResult(
                id=row["id"],
                level="L1",
                content=row["content"],
                score=float(row["confidence"]) * 0.25,
                memory_type=row["memory_type"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                source="recent",
            )
            for row in rows
        ]

    def touch(self, ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return
        now = utc_now_iso()
        with self.connect() as conn:
            for table in ("memory_atoms", "memory_blocks", "memory_profiles"):
                conn.executemany(
                    f"UPDATE {table} SET last_accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                    [(now, item_id) for item_id in ids],
                )

    def forget(self, scope: MemoryScope, query_or_ids: str, hard_delete: bool = False) -> int:
        ids = [part.strip() for part in query_or_ids.split(",") if part.strip()]
        if len(ids) == 1 and len(ids[0]) < 16:
            ids = [result.id for result in self.search(scope, query_or_ids, limit=20, include_archived=True)]
        if not ids:
            return 0
        with self.connect() as conn:
            total = 0
            for table, fts_table in (
                ("memory_atoms", "memory_atoms_fts"),
                ("memory_blocks", "memory_blocks_fts"),
                ("memory_profiles", "memory_profiles_fts"),
            ):
                if hard_delete:
                    for item_id in ids:
                        conn.execute(f"DELETE FROM {fts_table} WHERE id = ?", (item_id,))
                    result = conn.executemany(f"DELETE FROM {table} WHERE id = ?", [(item_id,) for item_id in ids])
                else:
                    result = conn.executemany(
                        f"UPDATE {table} SET status = ?, updated_at = ? WHERE id = ?",
                        [(MemoryStatus.DELETED.value, utc_now_iso(), item_id) for item_id in ids],
                    )
                total += result.rowcount if result.rowcount and result.rowcount > 0 else 0
            return total

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "turns": conn.execute("SELECT COUNT(*) FROM memory_turns").fetchone()[0],
                "atoms": conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE status = ?", (MemoryStatus.ACTIVE.value,)).fetchone()[0],
                "blocks": conn.execute("SELECT COUNT(*) FROM memory_blocks WHERE status = ?", (MemoryStatus.ACTIVE.value,)).fetchone()[0],
                "profiles": conn.execute("SELECT COUNT(*) FROM memory_profiles WHERE status = ?", (MemoryStatus.ACTIVE.value,)).fetchone()[0],
                "embeddings_pending": conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE embedding_status = 'pending'").fetchone()[0],
                "embeddings_ready": conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE embedding_status = 'ready'").fetchone()[0],
                "embeddings_failed": conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE embedding_status = 'failed'").fetchone()[0],
                "vectors": conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0],
                "jobs_pending": conn.execute(
                    "SELECT COUNT(*) FROM memory_jobs WHERE status IN (?, ?)",
                    (MemoryJobStatus.PENDING.value, MemoryJobStatus.RETRY.value),
                ).fetchone()[0],
            }

    def _query_terms(self, query: str) -> list[str]:
        cleaned = "".join(char if char.isalnum() or "一" <= char <= "鿿" else " " for char in query)
        terms = []
        for part in cleaned.split():
            part = part.strip()
            if not part:
                continue
            if len(part) > 32:
                part = part[:32]
            terms.append(part)
        if not terms and query.strip():
            terms.append(query.strip()[:32])
        return terms[:12]

    def _rank_to_score(self, rank: float, confidence: float) -> float:
        return (1.0 / (1.0 + abs(float(rank)))) * float(confidence)

    def _cosine_similarity(self, left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)
