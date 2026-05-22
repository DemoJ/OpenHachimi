"""长期记忆向量索引。"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any

from openhachimi_agent.memory.models import MemoryScope, MemorySearchResult, MemorySensitivity, MemoryStatus


def normalize_vector(vector: list[float]) -> tuple[list[float], float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return values, 0.0
    return [value / norm for value in values], norm


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_normed, left_norm = normalize_vector(left)
    right_normed, right_norm = normalize_vector(right)
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left_normed, right_normed))


def shard_keys_for_vector(vector: list[float], *, top_dims: int = 4, include_prefixes: bool = True) -> list[str]:
    normalized, norm = normalize_vector(vector)
    if norm == 0:
        return []
    ranked = sorted(enumerate(normalized), key=lambda item: abs(item[1]), reverse=True)[: max(1, top_dims)]
    parts = [f"d{index}{'+' if value >= 0 else '-'}" for index, value in ranked]
    if not include_prefixes:
        return ["|".join(parts)]
    return ["|".join(parts[:index]) for index in range(1, len(parts) + 1)]


def sqlite_vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    return True


class SQLiteVecIndex:
    def __init__(self) -> None:
        self.available = sqlite_vec_available()

    def load(self, conn: sqlite3.Connection) -> bool:
        if not self.available:
            return False
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            return False
        return True

    def save(self, conn: sqlite3.Connection, *, item_id: str, level: str, model: str, vector: list[float]) -> bool:
        if not vector or not self.load(conn):
            return False
        dimensions = len(vector)
        table = _vec_table_name(level, dimensions)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_vec_items(
                vec_id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL UNIQUE,
                level TEXT NOT NULL,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(embedding float[{dimensions}])")
        now = str(conn.execute("SELECT datetime('now')").fetchone()[0])
        conn.execute(
            """
            INSERT INTO memory_vec_items(item_id, level, model, dimensions, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET level=excluded.level, model=excluded.model, dimensions=excluded.dimensions, updated_at=excluded.updated_at
            """,
            (item_id, level, model, dimensions, now),
        )
        row = conn.execute("SELECT vec_id FROM memory_vec_items WHERE item_id = ?", (item_id,)).fetchone()
        if not row:
            return False
        import sqlite_vec

        conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (row["vec_id"],))
        conn.execute(f"INSERT INTO {table}(rowid, embedding) VALUES(?, ?)", (row["vec_id"], sqlite_vec.serialize_float32(vector)))
        return True

    def search(
        self,
        conn: sqlite3.Connection,
        *,
        scope: MemoryScope,
        query_vector: list[float],
        limit: int,
        model: str | None,
        levels: tuple[str, ...],
    ) -> list[MemorySearchResult]:
        if not query_vector or not self.load(conn):
            return []
        dimensions = len(query_vector)
        results: list[MemorySearchResult] = []
        for level in levels:
            table = _vec_table_name(level, dimensions)
            if not _table_exists(conn, table):
                continue
            results.extend(self._search_level(conn, table=table, scope=scope, query_vector=query_vector, limit=limit, model=model, level=level, dimensions=dimensions))
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:limit]

    def _search_level(
        self,
        conn: sqlite3.Connection,
        *,
        table: str,
        scope: MemoryScope,
        query_vector: list[float],
        limit: int,
        model: str | None,
        level: str,
        dimensions: int,
    ) -> list[MemorySearchResult]:
        import sqlite_vec

        if level == "L1":
            rows = conn.execute(
                f"""
                SELECT m.item_id, v.distance,
                       a.content, a.memory_type, a.confidence, a.updated_at, a.valid_until, a.decay_at, a.stability, a.keywords_json
                FROM {table} v
                JOIN memory_vec_items m ON m.vec_id = v.rowid
                JOIN memory_atoms a ON a.id = m.item_id
                WHERE v.embedding MATCH ? AND k = ?
                  AND m.level = ? AND m.dimensions = ? AND (? IS NULL OR m.model = ?)
                  AND a.status = ? AND a.tenant_id = ? AND a.user_id = ?
                  AND (a.role_name = ? OR a.role_name = '') AND a.sensitivity != ?
                ORDER BY v.distance
                """,
                (
                    sqlite_vec.serialize_float32(query_vector),
                    limit,
                    level,
                    dimensions,
                    model,
                    model,
                    MemoryStatus.ACTIVE.value,
                    scope.tenant_id,
                    scope.user_id,
                    scope.role_name,
                    MemorySensitivity.SECRET.value,
                ),
            ).fetchall()
            return [
                MemorySearchResult(
                    id=row["item_id"],
                    level="L1",
                    content=row["content"],
                    score=(1.0 / (1.0 + float(row["distance"]))) * float(row["confidence"]),
                    memory_type=row["memory_type"],
                    confidence=row["confidence"],
                    updated_at=row["updated_at"],
                    source="sqlite-vec",
                    metadata={"valid_until": row["valid_until"], "decay_at": row["decay_at"], "stability": row["stability"], "keywords": _load_json_array(row["keywords_json"])},
                )
                for row in rows
            ]
        if level == "L2":
            rows = conn.execute(
                f"""
                SELECT m.item_id, v.distance,
                       b.title, b.summary, b.block_type, b.confidence, b.updated_at, b.freshness_score, b.atom_ids_json
                FROM {table} v
                JOIN memory_vec_items m ON m.vec_id = v.rowid
                JOIN memory_blocks b ON b.id = m.item_id
                WHERE v.embedding MATCH ? AND k = ?
                  AND m.level = ? AND m.dimensions = ? AND (? IS NULL OR m.model = ?)
                  AND b.status = ? AND b.tenant_id = ? AND b.user_id = ? AND (b.role_name = ? OR b.role_name = '')
                ORDER BY v.distance
                """,
                (
                    sqlite_vec.serialize_float32(query_vector),
                    limit,
                    level,
                    dimensions,
                    model,
                    model,
                    MemoryStatus.ACTIVE.value,
                    scope.tenant_id,
                    scope.user_id,
                    scope.role_name,
                ),
            ).fetchall()
            return [
                MemorySearchResult(
                    id=row["item_id"],
                    level="L2",
                    content=f"{row['title']}：{row['summary']}",
                    score=(1.0 / (1.0 + float(row["distance"]))) * float(row["confidence"]) * 1.08,
                    memory_type=row["block_type"],
                    confidence=row["confidence"],
                    updated_at=row["updated_at"],
                    source="sqlite-vec",
                    metadata={"freshness_score": row["freshness_score"], "atom_ids": _load_json_array(row["atom_ids_json"])},
                )
                for row in rows
            ]
        return []


class SQLiteVectorShardIndex:
    def __init__(self, *, top_dims: int = 4, candidate_multiplier: int = 20) -> None:
        self.top_dims = top_dims
        self.candidate_multiplier = candidate_multiplier

    def save(self, conn: sqlite3.Connection, *, item_id: str, level: str, model: str, vector: list[float]) -> None:
        normalized, norm = normalize_vector(vector)
        keys = shard_keys_for_vector(normalized, top_dims=self.top_dims)
        now_row = conn.execute("SELECT datetime('now')").fetchone()
        now = str(now_row[0]) if now_row else ""
        conn.execute("DELETE FROM memory_vector_shards WHERE item_id = ?", (item_id,))
        for key in keys:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_vector_shards(
                    item_id, level, model, dimensions, shard_key, norm, vector_json, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (item_id, level, model, len(vector), key, norm, json.dumps(vector, ensure_ascii=False, separators=(",", ":")), now, now),
            )

    def search(
        self,
        conn: sqlite3.Connection,
        *,
        scope: MemoryScope,
        query_vector: list[float],
        limit: int,
        model: str | None,
        levels: tuple[str, ...],
    ) -> list[MemorySearchResult]:
        keys = shard_keys_for_vector(query_vector, top_dims=self.top_dims)
        if not keys:
            return []
        candidate_limit = max(limit * self.candidate_multiplier, 200)
        level_placeholders = ",".join("?" for _ in levels)
        key_placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"""
            SELECT DISTINCT s.item_id, s.level, s.vector_json, s.model,
                   a.content AS atom_content, a.memory_type AS atom_type, a.confidence AS atom_confidence,
                   a.updated_at AS atom_updated_at, a.valid_until, a.decay_at, a.stability, a.keywords_json,
                   b.title AS block_title, b.summary AS block_summary, b.block_type, b.confidence AS block_confidence,
                   b.updated_at AS block_updated_at, b.freshness_score, b.atom_ids_json
            FROM memory_vector_shards s
            LEFT JOIN memory_atoms a ON s.level = 'L1' AND a.id = s.item_id
            LEFT JOIN memory_blocks b ON s.level = 'L2' AND b.id = s.item_id
            WHERE s.level IN ({level_placeholders})
              AND s.shard_key IN ({key_placeholders})
              AND (? IS NULL OR s.model = ?)
              AND (
                    (s.level = 'L1' AND a.status = ? AND a.tenant_id = ? AND a.user_id = ? AND (a.role_name = ? OR a.role_name = '') AND a.sensitivity != ?)
                 OR (s.level = 'L2' AND b.status = ? AND b.tenant_id = ? AND b.user_id = ? AND (b.role_name = ? OR b.role_name = ''))
              )
            LIMIT ?
            """,
            (
                *levels,
                *keys,
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
                candidate_limit,
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
                        source="vector-shard",
                        metadata={
                            "valid_until": row["valid_until"],
                            "decay_at": row["decay_at"],
                            "stability": row["stability"],
                            "keywords": _load_json_array(row["keywords_json"]),
                        },
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
                        source="vector-shard",
                        metadata={
                            "freshness_score": row["freshness_score"],
                            "atom_ids": _load_json_array(row["atom_ids_json"]),
                        },
                    )
                )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]


def _vec_table_name(level: str, dimensions: int) -> str:
    safe_level = re.sub(r"[^a-zA-Z0-9_]", "_", level.lower())
    return f"memory_vec_{safe_level}_{dimensions}"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')", (table_name,)).fetchone()
    return row is not None


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
