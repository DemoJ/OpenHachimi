from __future__ import annotations

import json
import sqlite3
from typing import Any

from openhachimi_agent.memory.models import (
    MemoryBlock,
    MemoryJob,
    MemoryJobStatus,
    MemoryProfile,
    MemoryScope,
    MemoryStability,
    MemoryStatus,
)


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

def _job_from_row(row: sqlite3.Row) -> MemoryJob:
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
def _block_from_row(row: sqlite3.Row) -> MemoryBlock:
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
def _profile_from_row(row: sqlite3.Row) -> MemoryProfile:
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
