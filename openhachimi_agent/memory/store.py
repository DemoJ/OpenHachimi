"""长期记忆 SQLite 存储。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from openhachimi_agent.memory.models import (
    MemoryAtom,
    MemoryBlock,
    MemoryJob,
    MemoryJobStatus,
    MemoryProfile,
    MemoryScope,
    MemorySearchResult,
    MemorySensitivity,
    MemoryStability,
    MemoryStatus,
    MemoryTurn,
    utc_now_iso,
)
from openhachimi_agent.memory.vector_index import SQLiteVecIndex, SQLiteVectorShardIndex, cosine_similarity

SCHEMA_VERSION = 2


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


def _load_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _scope_from_row(row: sqlite3.Row) -> MemoryScope:
    return MemoryScope(
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        role_name=row["role_name"] or "",
        session_id=row["session_id"] if "session_id" in row.keys() else "",
        channel=row["channel"] if "channel" in row.keys() else "cli",
    )


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.vector_index = SQLiteVectorShardIndex()
        self.sqlite_vec_index = SQLiteVecIndex()
        self._local = threading.local()
        self.initialize()

    @contextmanager
    def connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
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

                CREATE TABLE IF NOT EXISTS memory_vector_shards (
                    item_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    shard_key TEXT NOT NULL,
                    norm REAL NOT NULL,
                    vector_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(item_id, shard_key)
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

                CREATE TABLE IF NOT EXISTS memory_block_atoms (
                    block_id TEXT NOT NULL,
                    atom_id TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'supports',
                    weight REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(block_id, atom_id)
                );

                CREATE TABLE IF NOT EXISTS memory_conflicts (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    conflict_key TEXT NOT NULL,
                    winner_id TEXT,
                    loser_id TEXT,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_memory_atoms_scope ON memory_atoms(tenant_id, user_id, role_name, status);
                CREATE INDEX IF NOT EXISTS idx_memory_atoms_due ON memory_atoms(status, valid_until, decay_at);
                CREATE INDEX IF NOT EXISTS idx_memory_atoms_consolidate ON memory_atoms(tenant_id, user_id, role_name, status, memory_type, updated_at);
                CREATE INDEX IF NOT EXISTS idx_memory_blocks_scope ON memory_blocks(tenant_id, user_id, role_name, status);
                CREATE INDEX IF NOT EXISTS idx_memory_blocks_consolidate ON memory_blocks(tenant_id, user_id, role_name, status, block_type, updated_at);
                CREATE INDEX IF NOT EXISTS idx_memory_profiles_scope ON memory_profiles(tenant_id, user_id, role_name, status);
                CREATE INDEX IF NOT EXISTS idx_memory_vectors_level ON memory_vectors(level, model);
                CREATE INDEX IF NOT EXISTS idx_memory_vector_shards_lookup ON memory_vector_shards(level, model, dimensions, shard_key);
                CREATE INDEX IF NOT EXISTS idx_memory_jobs_status ON memory_jobs(status, run_after);
                CREATE INDEX IF NOT EXISTS idx_memory_jobs_claim ON memory_jobs(status, run_after, locked_at);
                CREATE INDEX IF NOT EXISTS idx_memory_conflicts_scope ON memory_conflicts(tenant_id, user_id, role_name, conflict_key);
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

    def add_block(self, block: MemoryBlock) -> str:
        keywords_text = " ".join(block.keywords)
        search_text = block.search_text or " ".join([block.title, block.summary, block.details, keywords_text])
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
                    search_text,
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
            if str(block.status) == MemoryStatus.ACTIVE.value:
                conn.execute(
                    "INSERT INTO memory_blocks_fts(id, title, summary, details, search_text, keywords) VALUES(?,?,?,?,?,?)",
                    (block.id, block.title, block.summary, block.details, search_text, keywords_text),
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
            if str(profile.status) == MemoryStatus.ACTIVE.value:
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

    def enqueue_unique_job(self, job_type: str, payload: dict[str, Any], *, dedupe_key: str, run_after: str | None = None) -> str:
        with self.connect() as conn:
            try:
                row = conn.execute(
                    """
                    SELECT id FROM memory_jobs
                    WHERE job_type = ? AND status IN (?, ?, ?)
                      AND json_extract(payload_json, '$._dedupe_key') = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (job_type, MemoryJobStatus.PENDING.value, MemoryJobStatus.RETRY.value, MemoryJobStatus.RUNNING.value, dedupe_key),
                ).fetchone()
            except sqlite3.OperationalError:
                rows = conn.execute(
                    """
                    SELECT id, payload_json FROM memory_jobs
                    WHERE job_type = ? AND status IN (?, ?, ?)
                    ORDER BY created_at DESC
                    """,
                    (job_type, MemoryJobStatus.PENDING.value, MemoryJobStatus.RETRY.value, MemoryJobStatus.RUNNING.value),
                ).fetchall()
                row = next((item for item in rows if _load_json_dict(item["payload_json"]).get("_dedupe_key") == dedupe_key), None)
            if row:
                return str(row["id"])
        full_payload = dict(payload)
        full_payload["_dedupe_key"] = dedupe_key
        return self.enqueue_job(MemoryJob(job_type=job_type, payload=full_payload, run_after=run_after or utc_now_iso()))

    def claim_due_jobs(self, limit: int = 10, *, lock_seconds: int = 300) -> list[MemoryJob]:
        now = utc_now_iso()
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=lock_seconds)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                UPDATE memory_jobs
                SET status = ?, attempts = attempts + 1, locked_at = ?, updated_at = ?
                WHERE id IN (
                    SELECT id FROM memory_jobs
                    WHERE (status IN (?, ?) AND run_after <= ?)
                       OR (status = ? AND locked_at IS NOT NULL AND locked_at <= ?)
                    ORDER BY run_after, created_at
                    LIMIT ?
                )
                RETURNING *
                """,
                (
                    MemoryJobStatus.RUNNING.value,
                    now,
                    now,
                    MemoryJobStatus.PENDING.value,
                    MemoryJobStatus.RETRY.value,
                    now,
                    MemoryJobStatus.RUNNING.value,
                    stale_before,
                    limit,
                ),
            ).fetchall()
            return [self._job_from_row(row) for row in rows]

    def complete_job(self, job_id: str) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE memory_jobs SET status = ?, locked_at = NULL, last_error = '', updated_at = ? WHERE id = ?",
                (MemoryJobStatus.SUCCEEDED.value, now, job_id),
            )

    def fail_job(self, job_id: str, error: str, *, retry_delay_seconds: int = 60) -> None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self.connect() as conn:
            row = conn.execute("SELECT attempts, max_attempts FROM memory_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return
            if int(row["attempts"]) < int(row["max_attempts"]):
                status = MemoryJobStatus.RETRY.value
                run_after = (now_dt + timedelta(seconds=retry_delay_seconds * max(1, int(row["attempts"])))).isoformat()
            else:
                status = MemoryJobStatus.FAILED.value
                run_after = now
            conn.execute(
                """
                UPDATE memory_jobs
                SET status = ?, run_after = ?, locked_at = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, run_after, error[:1000], now, job_id),
            )

    def set_atom_embedding_status(self, atom_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE memory_atoms SET embedding_status = ?, updated_at = ? WHERE id = ?", (status, utc_now_iso(), atom_id))

    def set_block_embedding_status(self, block_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE memory_blocks SET embedding_status = ?, updated_at = ? WHERE id = ?", (status, utc_now_iso(), block_id))

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
            self.sqlite_vec_index.save(conn, item_id=item_id, level=level, model=model, vector=vector)
            self.vector_index.save(conn, item_id=item_id, level=level, model=model, vector=vector)
            if level == "L1":
                conn.execute("UPDATE memory_atoms SET embedding_status = ?, updated_at = ? WHERE id = ?", ("ready", now, item_id))
            elif level == "L2":
                conn.execute("UPDATE memory_blocks SET embedding_status = ?, updated_at = ? WHERE id = ?", ("ready", now, item_id))

    def get_atom_content(self, atom_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT content FROM memory_atoms WHERE id = ?", (atom_id,)).fetchone()
            return str(row["content"]) if row else None

    def vector_search(
        self,
        scope: MemoryScope,
        query_vector: list[float],
        limit: int = 10,
        model: str | None = None,
        levels: tuple[str, ...] = ("L1", "L2"),
    ) -> list[MemorySearchResult]:
        if not query_vector:
            return []
        limit = max(1, min(limit, 50))
        with self.connect() as conn:
            results: list[MemorySearchResult] = []
            results.extend(self.sqlite_vec_index.search(conn, scope=scope, query_vector=query_vector, limit=limit, model=model, levels=levels))
            shard_count = conn.execute("SELECT COUNT(*) FROM memory_vector_shards").fetchone()[0]
            if shard_count:
                results.extend(self.vector_index.search(conn, scope=scope, query_vector=query_vector, limit=limit, model=model, levels=levels))
            results.extend(self._legacy_vector_search(conn, scope, query_vector, limit, model, levels))
        best_by_id: dict[str, MemorySearchResult] = {}
        for result in results:
            existing = best_by_id.get(result.id)
            if existing is None:
                best_by_id[result.id] = result
            else:
                existing_sources = existing.source.split("+")
                if result.source not in existing_sources:
                    existing.source = f"{existing.source}+{result.source}"
                if result.score > existing.score:
                    result.source = existing.source
                    best_by_id[result.id] = result
        merged = sorted(best_by_id.values(), key=lambda item: item.score, reverse=True)
        return merged[:limit]

    def search(self, scope: MemoryScope, query: str, limit: int = 10, include_archived: bool = False, touch_results: bool = True) -> list[MemorySearchResult]:
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
        if touch_results:
            self.touch([item.id for item in final])
        return final

    def list_atoms_for_consolidation(self, scope: MemoryScope | None = None, *, limit: int = 200, min_confidence: float = 0.55) -> list[sqlite3.Row]:
        now = utc_now_iso()
        scope_sql = ""
        params: list[Any] = [MemoryStatus.ACTIVE.value, MemorySensitivity.SECRET.value, min_confidence, now]
        if scope:
            scope_sql = "AND tenant_id = ? AND user_id = ? AND (role_name = ? OR role_name = '')"
            params.extend([scope.tenant_id, scope.user_id, scope.role_name])
        params.append(limit)
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT * FROM memory_atoms
                WHERE status = ? AND sensitivity != ? AND confidence >= ?
                  AND (valid_until IS NULL OR valid_until > ?)
                  {scope_sql}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()

    def list_blocks_for_profile_consolidation(self, scope: MemoryScope | None = None, *, limit: int = 50) -> list[sqlite3.Row]:
        scope_sql = ""
        params: list[Any] = [MemoryStatus.ACTIVE.value]
        if scope:
            scope_sql = "AND tenant_id = ? AND user_id = ? AND (role_name = ? OR role_name = '')"
            params.extend([scope.tenant_id, scope.user_id, scope.role_name])
        params.append(limit)
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT * FROM memory_blocks
                WHERE status = ? {scope_sql}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()

    def get_active_block_by_topic(self, scope: MemoryScope, block_type: str, topic_key: str) -> MemoryBlock | None:
        title = f"{block_type}: {topic_key}"
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_blocks
                WHERE status = ? AND tenant_id = ? AND user_id = ? AND role_name = ? AND block_type = ? AND title = ?
                LIMIT 1
                """,
                (MemoryStatus.ACTIVE.value, scope.tenant_id, scope.user_id, scope.role_name, block_type, title),
            ).fetchone()
            return self._block_from_row(row) if row else None

    def get_active_profile(self, tenant_id: str, user_id: str, role_name: str | None, profile_type: str) -> MemoryProfile | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_profiles
                WHERE status = ? AND tenant_id = ? AND user_id = ? AND (role_name = ? OR role_name IS NULL) AND profile_type = ?
                ORDER BY role_name DESC, updated_at DESC
                LIMIT 1
                """,
                (MemoryStatus.ACTIVE.value, tenant_id, user_id, role_name, profile_type),
            ).fetchone()
            return self._profile_from_row(row) if row else None

    def link_block_atoms(self, block_id: str, atom_ids: list[str], *, relation: str = "supports") -> None:
        now = utc_now_iso()
        unique_ids = list(dict.fromkeys(atom_ids))
        with self.connect() as conn:
            for atom_id in unique_ids:
                conn.execute(
                    "INSERT OR REPLACE INTO memory_block_atoms(block_id, atom_id, relation, weight, created_at) VALUES(?,?,?,?,?)",
                    (block_id, atom_id, relation, 1.0, now),
                )
            all_ids = [
                row["atom_id"]
                for row in conn.execute("SELECT atom_id FROM memory_block_atoms WHERE block_id = ? ORDER BY created_at", (block_id,)).fetchall()
            ]
            conn.execute("UPDATE memory_blocks SET atom_ids_json = ?, updated_at = ? WHERE id = ?", (_json(all_ids), now, block_id))

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

    def update_atom_status(self, atom_id: str, status: MemoryStatus | str) -> None:
        status_value = str(status)
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("UPDATE memory_atoms SET status = ?, updated_at = ? WHERE id = ?", (status_value, now, atom_id))
            if status_value != MemoryStatus.ACTIVE.value:
                conn.execute("DELETE FROM memory_atoms_fts WHERE id = ?", (atom_id,))

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

    def archive_decayed_atoms(self, *, now: str | None = None, limit: int = 500) -> int:
        current = now or utc_now_iso()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM memory_atoms
                WHERE status = ? AND decay_at IS NOT NULL AND decay_at <= ?
                  AND stability = 'ephemeral' AND access_count = 0
                LIMIT ?
                """,
                (MemoryStatus.ACTIVE.value, current, limit),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            conn.executemany(
                "UPDATE memory_atoms SET status = ?, updated_at = ? WHERE id = ?",
                [(MemoryStatus.ARCHIVED.value, current, atom_id) for atom_id in ids],
            )
            conn.executemany("DELETE FROM memory_atoms_fts WHERE id = ?", [(atom_id,) for atom_id in ids])
            return len(ids)

    def list_memories(self, scope: MemoryScope, memory_type: str | None = None, limit: int = 20, include_archived: bool = False) -> list[MemorySearchResult]:
        limit = max(1, min(limit, 100))
        statuses = [MemoryStatus.ACTIVE.value]
        if include_archived:
            statuses.append(MemoryStatus.ARCHIVED.value)
        placeholders = ",".join("?" for _ in statuses)
        type_filter_atoms = "AND memory_type = ?" if memory_type else ""
        type_filter_blocks = "AND block_type = ?" if memory_type else ""
        type_filter_profiles = "AND profile_type = ?" if memory_type else ""
        atom_params: list[Any] = [*statuses, scope.tenant_id, scope.user_id, scope.role_name, MemorySensitivity.SECRET.value]
        block_params: list[Any] = [*statuses, scope.tenant_id, scope.user_id, scope.role_name]
        profile_params: list[Any] = [*statuses, scope.tenant_id, scope.user_id, scope.role_name]
        if memory_type:
            atom_params.append(memory_type)
            block_params.append(memory_type)
            profile_params.append(memory_type)
        with self.connect() as conn:
            atom_rows = conn.execute(
                f"""
                SELECT * FROM memory_atoms
                WHERE status IN ({placeholders}) AND tenant_id = ? AND user_id = ?
                  AND (role_name = ? OR role_name = '') AND sensitivity != ? {type_filter_atoms}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*atom_params, limit),
            ).fetchall()
            block_rows = conn.execute(
                f"""
                SELECT * FROM memory_blocks
                WHERE status IN ({placeholders}) AND tenant_id = ? AND user_id = ?
                  AND (role_name = ? OR role_name = '') {type_filter_blocks}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*block_params, limit),
            ).fetchall()
            profile_rows = conn.execute(
                f"""
                SELECT * FROM memory_profiles
                WHERE status IN ({placeholders}) AND tenant_id = ? AND user_id = ?
                  AND (role_name = ? OR role_name IS NULL) {type_filter_profiles}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*profile_params, limit),
            ).fetchall()
        results = [
            MemorySearchResult(
                id=row["id"],
                level="L1",
                content=row["content"],
                score=float(row["confidence"]),
                memory_type=row["memory_type"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                source="list",
                metadata={"keywords": _load_json_array(row["keywords_json"]), "valid_until": row["valid_until"], "decay_at": row["decay_at"], "stability": row["stability"]},
            )
            for row in atom_rows
        ]
        results.extend(
            MemorySearchResult(
                id=row["id"],
                level="L2",
                content=f"{row['title']}：{row['summary']}",
                score=float(row["confidence"]) * 1.08,
                memory_type=row["block_type"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                source="list",
                metadata={"freshness_score": row["freshness_score"], "atom_ids": _load_json_array(row["atom_ids_json"])},
            )
            for row in block_rows
        )
        results.extend(
            MemorySearchResult(
                id=row["id"],
                level="L3",
                content=f"{row['title']}：{row['summary']}",
                score=float(row["confidence"]) * 1.15,
                memory_type=row["profile_type"],
                confidence=row["confidence"],
                updated_at=row["updated_at"],
                source="list",
                metadata={"evidence_atom_ids": _load_json_array(row["evidence_atom_ids_json"]), "stability": row["stability"]},
            )
            for row in profile_rows
        )
        results.sort(key=lambda item: item.updated_at, reverse=True)
        final = results[:limit]
        self.touch([item.id for item in final])
        return final

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
        now = utc_now_iso()
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
                        [(MemoryStatus.DELETED.value, now, item_id) for item_id in ids],
                    )
                    conn.executemany(f"DELETE FROM {fts_table} WHERE id = ?", [(item_id,) for item_id in ids])
                total += result.rowcount if result.rowcount and result.rowcount > 0 else 0
            if hard_delete:
                conn.executemany("DELETE FROM memory_vectors WHERE item_id = ?", [(item_id,) for item_id in ids])
                conn.executemany("DELETE FROM memory_vector_shards WHERE item_id = ?", [(item_id,) for item_id in ids])
                conn.executemany("DELETE FROM memory_block_atoms WHERE atom_id = ? OR block_id = ?", [(item_id, item_id) for item_id in ids])
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
                "vector_shards": conn.execute("SELECT COUNT(*) FROM memory_vector_shards").fetchone()[0],
                "jobs_pending": conn.execute(
                    "SELECT COUNT(*) FROM memory_jobs WHERE status IN (?, ?)",
                    (MemoryJobStatus.PENDING.value, MemoryJobStatus.RETRY.value),
                ).fetchone()[0],
            }

    def _legacy_vector_search(
        self,
        conn: sqlite3.Connection,
        scope: MemoryScope,
        query_vector: list[float],
        limit: int,
        model: str | None,
        levels: tuple[str, ...],
    ) -> list[MemorySearchResult]:
        level_placeholders = ",".join("?" for _ in levels)
        rows = conn.execute(
            f"""
            SELECT v.item_id, v.level, v.vector_json, v.model,
                   a.content AS atom_content, a.memory_type AS atom_type, a.confidence AS atom_confidence,
                   a.updated_at AS atom_updated_at, a.valid_until, a.decay_at, a.stability, a.keywords_json,
                   b.title AS block_title, b.summary AS block_summary, b.block_type, b.confidence AS block_confidence,
                   b.updated_at AS block_updated_at, b.freshness_score, b.atom_ids_json
            FROM memory_vectors v
            LEFT JOIN memory_atoms a ON v.level = 'L1' AND a.id = v.item_id
            LEFT JOIN memory_blocks b ON v.level = 'L2' AND b.id = v.item_id
            WHERE v.level IN ({level_placeholders})
              AND (? IS NULL OR v.model = ?)
              AND (
                    (v.level = 'L1' AND a.status = ? AND a.tenant_id = ? AND a.user_id = ? AND (a.role_name = ? OR a.role_name = '') AND a.sensitivity != ?)
                 OR (v.level = 'L2' AND b.status = ? AND b.tenant_id = ? AND b.user_id = ? AND (b.role_name = ? OR b.role_name = ''))
              )
            """,
            (
                *levels,
                model,
                model,
                MemoryStatus.ACTIVE.value,
                scope.tenant_id,
                scope.user_id,
                scope.role_name,
                MemorySensitivity.SECRET.value,
                MemoryStatus.ACTIVE.value,
                scope.tenant_id,
                scope.user_id,
                scope.role_name,
            ),
        ).fetchall()
        scored: list[MemorySearchResult] = []
        for row in rows:
            try:
                vector = [float(value) for value in json.loads(row["vector_json"])]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            similarity = cosine_similarity(query_vector, vector)
            if similarity <= 0:
                continue
            if row["level"] == "L1":
                confidence = float(row["atom_confidence"])
                scored.append(
                    MemorySearchResult(
                        id=row["item_id"],
                        level="L1",
                        content=row["atom_content"],
                        score=similarity * confidence,
                        memory_type=row["atom_type"],
                        confidence=confidence,
                        updated_at=row["atom_updated_at"],
                        source="vector",
                        metadata={"valid_until": row["valid_until"], "decay_at": row["decay_at"], "stability": row["stability"], "keywords": _load_json_array(row["keywords_json"])},
                    )
                )
            elif row["level"] == "L2":
                confidence = float(row["block_confidence"])
                scored.append(
                    MemorySearchResult(
                        id=row["item_id"],
                        level="L2",
                        content=f"{row['block_title']}：{row['block_summary']}",
                        score=similarity * confidence * 1.08,
                        memory_type=row["block_type"],
                        confidence=confidence,
                        updated_at=row["block_updated_at"],
                        source="vector",
                        metadata={"freshness_score": row["freshness_score"], "atom_ids": _load_json_array(row["atom_ids_json"])},
                    )
                )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]

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
                metadata={
                    "keywords": _load_json_array(row["keywords_json"]),
                    "valid_until": row["valid_until"],
                    "decay_at": row["decay_at"],
                    "stability": row["stability"],
                    "evidence_turn_ids": _load_json_array(row["evidence_turn_ids_json"]),
                },
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
                metadata={"freshness_score": row["freshness_score"], "atom_ids": _load_json_array(row["atom_ids_json"]), "keywords": _load_json_array(row["keywords_json"])},
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
                metadata={"evidence_atom_ids": _load_json_array(row["evidence_atom_ids_json"]), "stability": row["stability"]},
            )
            for row in rows
        ]

    def _search_like(self, conn: sqlite3.Connection, scope: MemoryScope, terms: list[str], statuses: list[str], limit: int) -> list[MemorySearchResult]:
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
                metadata={"valid_until": row["valid_until"], "decay_at": row["decay_at"], "stability": row["stability"], "keywords": _load_json_array(row["keywords_json"])},
            )
            for row in rows
        ]

    def _recent_atoms(self, conn: sqlite3.Connection, scope: MemoryScope, statuses: list[str], limit: int) -> list[MemorySearchResult]:
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
                metadata={"valid_until": row["valid_until"], "decay_at": row["decay_at"], "stability": row["stability"], "keywords": _load_json_array(row["keywords_json"])},
            )
            for row in rows
        ]

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

    def _job_from_row(self, row: sqlite3.Row) -> MemoryJob:
        return MemoryJob(
            id=row["id"],
            job_type=row["job_type"],
            payload=_load_json_dict(row["payload_json"]),
            status=MemoryJobStatus(row["status"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            run_after=row["run_after"],
            locked_at=row["locked_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _block_from_row(self, row: sqlite3.Row) -> MemoryBlock:
        return MemoryBlock(
            id=row["id"],
            block_type=row["block_type"],
            title=row["title"],
            summary=row["summary"],
            details=row["details"],
            search_text=row["search_text"],
            atom_ids=_load_json_array(row["atom_ids_json"]),
            entities=_load_json_array(row["entities_json"]),
            keywords=_load_json_array(row["keywords_json"]),
            tags=_load_json_array(row["tags_json"]),
            scope=_scope_from_row(row),
            confidence=row["confidence"],
            coherence_score=row["coherence_score"],
            freshness_score=row["freshness_score"],
            status=MemoryStatus(row["status"]),
            last_consolidated_at=row["last_consolidated_at"],
            embedding_status=row["embedding_status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_accessed_at=row["last_accessed_at"],
            access_count=row["access_count"],
        )

    def _profile_from_row(self, row: sqlite3.Row) -> MemoryProfile:
        return MemoryProfile(
            id=row["id"],
            profile_type=row["profile_type"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            role_name=row["role_name"],
            title=row["title"],
            summary=row["summary"],
            traits=_load_json_dict(row["traits_json"]),
            preferences=_load_json_dict(row["preferences_json"]),
            constraints=_load_json_dict(row["constraints_json"]),
            dislikes=_load_json_dict(row["dislikes_json"]),
            evidence_atom_ids=_load_json_array(row["evidence_atom_ids_json"]),
            confidence=row["confidence"],
            stability=MemoryStability(row["stability"]),
            status=MemoryStatus(row["status"]),
            last_reviewed_at=row["last_reviewed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_accessed_at=row["last_accessed_at"],
            access_count=row["access_count"],
        )
