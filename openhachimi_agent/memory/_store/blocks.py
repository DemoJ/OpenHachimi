import sqlite3
from typing import Any

from openhachimi_agent.memory.models import (
    MemoryBlock,
    MemoryScope,
    MemorySensitivity,
    MemoryStatus,
    utc_now_iso,
)
from openhachimi_agent.memory._store.utils import (
    _block_from_row,
    _json,
)


class BlockStoreMixin:
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

    def set_block_embedding_status(self, block_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE memory_blocks SET embedding_status = ?, updated_at = ? WHERE id = ?", (status, utc_now_iso(), block_id))

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
            return _block_from_row(row) if row else None

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
