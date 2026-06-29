from __future__ import annotations

import json
import sqlite3

from openhachimi_agent.memory.models import (
    MemoryScope,
    MemorySearchResult,
    MemorySensitivity,
    MemoryStatus,
    utc_now_iso,
)
from openhachimi_agent.memory._store.utils import (
    _json,
    _load_json_array,
)
from openhachimi_agent.memory.vector_index import cosine_similarity


class VectorStoreMixin:
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
