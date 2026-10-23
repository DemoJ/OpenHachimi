import sqlite3
from typing import Any

from openhachimi_agent.memory.models import (
    MemoryScope,
    MemorySearchResult,
    MemorySensitivity,
    MemoryStatus,
    utc_now_iso,
)
from openhachimi_agent.memory._store.fts import match_expression, query_terms
from openhachimi_agent.memory._store.utils import _load_json_array


class SearchStoreMixin:
    def search(self, scope: MemoryScope, query: str, limit: int = 10, include_archived: bool = False, touch_results: bool = True) -> list[MemorySearchResult]:
        limit = max(1, min(limit, 50))
        terms = self._query_terms(query)
        if not terms:
            return []
        fts_query = match_expression(terms)
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

    def list_memories(self, scope: MemoryScope, memory_type: str | None = None, limit: int = 20, include_archived: bool = False, touch: bool = True) -> list[MemorySearchResult]:
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
        # touch=False 时仅查阅不更新访问态——HTTP 管理页列出记忆不应污染
        # access_count/last_accessed_at(那会干扰衰减打分逻辑)。agent 工具默认 True。
        if touch:
            self.touch([item.id for item in final])
        return final

    def list_core_atoms(self, scope: MemoryScope, memory_types: frozenset[str], *, limit: int) -> list[MemorySearchResult]:
        """常驻记忆:指定类型里置信度最高的 active 记忆(未过期、非 secret),不需要查询词。"""
        if limit <= 0 or not memory_types:
            return []
        types = sorted(memory_types)
        type_placeholders = ",".join("?" for _ in types)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memory_atoms
                WHERE status = ? AND tenant_id = ? AND user_id = ?
                  AND (role_name = ? OR role_name = '') AND sensitivity != ?
                  AND memory_type IN ({type_placeholders})
                  AND (valid_until IS NULL OR valid_until > ?)
                ORDER BY confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (MemoryStatus.ACTIVE.value, scope.tenant_id, scope.user_id, scope.role_name,
                 MemorySensitivity.SECRET.value, *types, utc_now_iso(), limit),
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
                source="core",
                metadata={"valid_until": row["valid_until"], "decay_at": row["decay_at"], "stability": row["stability"]},
            )
            for row in rows
        ]

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
        # 汉字切成二元组、其余按字母数字片段切分,与写入索引的切法一致(见 _store/fts.py)。
        # 全是标点/特殊字符时返回空 list,由调用方直接返回空结果。
        return query_terms(query)

    def _rank_to_score(self, rank: float, confidence: float) -> float:
        # FTS5 的 bm25() 越匹配越负(绝对值越大),所以分数要随 |rank| 增大而增大,
        # 映射到 [0, 1) 后再乘置信度。旧实现用 1/(1+|rank|),把排序整个反了过来。
        magnitude = abs(float(rank))
        return (magnitude / (1.0 + magnitude)) * float(confidence)
