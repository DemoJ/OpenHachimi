import sqlite3
from typing import Any

from openhachimi_agent.memory.models import (
    MemoryProfile,
    MemoryScope,
    MemoryStatus,
)
from openhachimi_agent.memory._store.utils import (
    _json,
    _profile_from_row,
)


class ProfileStoreMixin:
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
            return _profile_from_row(row) if row else None
