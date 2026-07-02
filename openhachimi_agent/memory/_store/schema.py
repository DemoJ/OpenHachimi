from __future__ import annotations

from openhachimi_agent.memory.models import utc_now_iso

SCHEMA_VERSION = 3


def _column_exists(conn, table: str, column: str) -> bool:
    """探测 SQLite 表是否已有指定列(ADD COLUMN 不支持 IF NOT EXISTS)。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """列缺失时幂等补列。``ddl`` 为 ADD COLUMN 的列定义,如 ``"TEXT NOT NULL DEFAULT 'user'"``。"""
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


class SchemaStoreMixin:
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
                    source TEXT NOT NULL DEFAULT 'user',
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
                """
            )
            # 旧库幂等补列:memory_turns.source 在 schema v2 建表语句里新增,
            # 已存在的旧库需 ALTER 补上,默认 'user' 与模型字段一致。
            _ensure_column(conn, "memory_turns", "source", "TEXT NOT NULL DEFAULT 'user'")
            # v3 migration:移除 supersede 机制遗留的列与表,把历史 superseded
            # atom 降级为 archived。幂等:已应用过 v3 的库不再执行。
            self._migrate_to_v3(conn)
            conn.execute(
                "INSERT OR IGNORE INTO memory_schema_migrations(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, utc_now_iso()),
            )

    def _migrate_to_v3(self, conn) -> None:
        """v3:移除 supersede 遗留 (列/表),历史 superseded atom 降级 archived。

        幂等:``memory_schema_migrations`` 已记 version=3 则跳过。对旧 v2 库:
        - 把 ``status='superseded'`` 的 atom 改成 ``archived``(SUPERSEDED 枚举已删,
          不迁移会变成无法识别的 status 值,被 recall/store 当作非 active 跳过);
        - ``ALTER TABLE memory_atoms DROP COLUMN`` 删 supersedes_id / superseded_by_id /
          conflict_group_id 三列(SQLite 3.35+ 支持 DROP COLUMN);
        - ``DROP TABLE memory_conflicts`` 及其索引(supersede 删除后该表无写入无读取)。
        全程在单连接内执行,initialize 的 connect() 上下文会统一提交。
        """
        already = conn.execute(
            "SELECT 1 FROM memory_schema_migrations WHERE version = ?", (3,)
        ).fetchone()
        if already:
            return

        # 建表已用新 schema(无三列/无 conflicts 表),旧库才需要这三列探测。
        supersede_cols = (
            "supersedes_id",
            "superseded_by_id",
            "conflict_group_id",
        )
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(memory_atoms)").fetchall()
        }
        for column in supersede_cols:
            if column in existing_cols:
                # 先把残留的 superseded atom 降级为 archived,避免删列后丢失语义
                if column == "superseded_by_id":
                    conn.execute(
                        "UPDATE memory_atoms SET status = 'archived' WHERE status = 'superseded'"
                    )
                conn.execute(f"ALTER TABLE memory_atoms DROP COLUMN {column}")

        # memory_conflicts 表仅在旧库存在(新建库不再创建);有则删
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_conflicts'"
        ).fetchone()
        if table_exists:
            conn.execute("DROP INDEX IF EXISTS idx_memory_conflicts_scope")
            conn.execute("DROP TABLE memory_conflicts")
