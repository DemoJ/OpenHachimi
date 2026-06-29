from collections.abc import Iterable

from openhachimi_agent.memory.models import (
    MemoryJobStatus,
    MemoryScope,
    MemoryStatus,
    utc_now_iso,
)


class LifecycleStoreMixin:
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
            # 不像是 ID（短于 32 位 hex 的一半），尝试 FTS 模糊匹配兜底；search()
            # 自身会清洗 FTS5 特殊字符并在无效输入时返回空列表，不再抛
            # "unknown special query"。
            try:
                ids = [result.id for result in self.search(scope, query_or_ids, limit=20, include_archived=True)]
            except Exception:
                ids = []
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
                # 仅统计 active 记忆:被取代(superseded)/软删除(deleted)/过期(expired)的 atom
                # 仅改 status、不碰 embedding_status,若不过滤会把这些死记忆的 pending/failed 一并计入,
                # 让"待向量化"虚高且永不清零。与下面 stats.atoms 的 active 过滤口径保持一致。
                "embeddings_pending": conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE embedding_status = 'pending' AND status = ?", (MemoryStatus.ACTIVE.value,)).fetchone()[0],
                "embeddings_ready": conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE embedding_status = 'ready' AND status = ?", (MemoryStatus.ACTIVE.value,)).fetchone()[0],
                "embeddings_failed": conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE embedding_status = 'failed' AND status = ?", (MemoryStatus.ACTIVE.value,)).fetchone()[0],
                "vectors": conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0],
                "vector_shards": conn.execute("SELECT COUNT(*) FROM memory_vector_shards").fetchone()[0],
                "jobs_pending": conn.execute(
                    "SELECT COUNT(*) FROM memory_jobs WHERE status IN (?, ?)",
                    (MemoryJobStatus.PENDING.value, MemoryJobStatus.RETRY.value),
                ).fetchone()[0],
            }
