"""v3 schema migration:移除 supersede 机制遗留。

验证旧 v2 库(memory_atoms 含 supersedes_id/superseded_by_id/conflict_group_id 三列、
存在 memory_conflicts 表、可能有 status='superseded' 的 atom)经 ``MemoryStore``
初始化后:三列被删、memory_conflicts 表被删、superseded atom 降级为 archived、
migrations 表记录 version=3。并验证迁移幂等(再次初始化不报错、不重复执行)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openhachimi_agent.memory.store import MemoryStore

FIXTURE = Path(__file__).parent.parent / "fixtures" / "memory_schema_v2.sql"


def _build_legacy_v2_db(db_path: Path) -> None:
    """用 v2 schema 建库,并塞入一条 superseded atom + 一条 conflict 记录。"""
    sql = FIXTURE.read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(sql)
        # 标记为已应用 v2(模拟旧库状态,触发 v3 迁移)
        conn.execute("INSERT OR IGNORE INTO memory_schema_migrations(version, applied_at) VALUES(?, ?)", (2, "2024-01-01T00:00:00+00:00"))
        # 插入一条 status='superseded' 的 atom(v3 已删除该枚举,迁移应降级为 archived)
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
            """,
            ("a1", "local", "local", "default", "s1", "cli", "preference",
             "user", "states", "中文回答", "用户喜欢中文回答", "用户喜欢中文回答", "x",
             "[]", "", "[]", "[]", "[]", "{}", 0.8, "situational", "personal", None,
             None, None, "superseded", None, "a2", "g1", "pending", "t", "t", None, 0),
        )
        # 插入一条 conflict 记录(v3 应删除整个 memory_conflicts 表)
        conn.execute(
            "INSERT INTO memory_conflicts(id, tenant_id, user_id, role_name, conflict_key, winner_id, loser_id, status, reason, created_at, resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("c1", "local", "local", "default", "k", "a2", "a1", "resolved", "test", "t", "t"),
        )
        conn.commit()
    finally:
        conn.close()


def test_v3_migration_drops_supersede_columns_and_conflicts_table(tmp_path):
    db = tmp_path / "ltm.sqlite3"
    _build_legacy_v2_db(db)

    # 打开 store 触发 initialize() → v3 migration
    MemoryStore(db)

    conn = sqlite3.connect(db)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)").fetchall()]
        assert "supersedes_id" not in cols
        assert "superseded_by_id" not in cols
        assert "conflict_group_id" not in cols

        conflicts = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_conflicts'"
        ).fetchone()
        assert conflicts is None  # memory_conflicts 表已删除

        # superseded atom 降级为 archived(枚举 SUPERSEDED 已删,旧值必须迁走)
        status = conn.execute("SELECT status FROM memory_atoms WHERE id = 'a1'").fetchone()[0]
        assert status == "archived"

        versions = [row[0] for row in conn.execute("SELECT version FROM memory_schema_migrations ORDER BY version")]
        assert 3 in versions
    finally:
        conn.close()


def test_v3_migration_is_idempotent(tmp_path):
    db = tmp_path / "ltm.sqlite3"
    _build_legacy_v2_db(db)

    MemoryStore(db)  # 首次迁移
    MemoryStore(db)  # 再次初始化不应报错也不重复执行

    conn = sqlite3.connect(db)
    try:
        versions = conn.execute("SELECT version FROM memory_schema_migrations ORDER BY version").fetchall()
        v3_count = sum(1 for row in versions if row[0] == 3)
        assert v3_count == 1  # INSERT OR IGNORE 保证只一条 v3 记录
    finally:
        conn.close()


def test_fresh_db_is_v3_schema(tmp_path):
    """全新库直接初始化为 v3,本就不含三列与 conflicts 表。"""
    db = tmp_path / "ltm.sqlite3"
    MemoryStore(db)

    conn = sqlite3.connect(db)
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)").fetchall()]
        assert "supersedes_id" not in cols
        assert "conflict_group_id" not in cols
        conflicts = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_conflicts'"
        ).fetchone()
        assert conflicts is None
        versions = [row[0] for row in conn.execute("SELECT version FROM memory_schema_migrations ORDER BY version")]
        assert 3 in versions
    finally:
        conn.close()