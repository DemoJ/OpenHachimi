"""清理历史持久化数据中的明文密钥(一次性安全维护)。

背景:``save_messages`` 等写入路径曾把消息原文(含用户让 Agent 读取的
config.yaml 内容)未经脱敏直接落库,导致活跃 API key/Token 明文留存于
SQLite。写入侧已改为落库前过 ``redact_text``,本模块负责清理存量:

- 逐表逐列扫描所有 TEXT 值,命中敏感模式即用脱敏结果重写(按 rowid 定位);
- FTS 影子表 / 向量分片表 / 系统表跳过,内容表清理后触发对应 FTS rebuild;
- 清理前先 WAL checkpoint + 整文件备份(``*.pre-clean-secrets.bak``),
  清理后 VACUUM 抹除空闲页中残留的旧文本。
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from openhachimi_agent.core.redaction import redact_persisted_data, redact_text

logger = logging.getLogger(__name__)

# 这些内部表不直接清理:FTS/向量影子表的数据由内容表 rebuild 重新生成,
# 系统迁移表不含用户文本。
_SKIP_TABLE_MARKERS = ("_fts", "sqlite_", "memory_vec_", "memory_schema_migrations")

BACKUP_SUFFIX = ".pre-clean-secrets.bak"


def _is_skipped_table(table: str) -> bool:
    return any(marker in table for marker in _SKIP_TABLE_MARKERS)


def _checkpoint(db_path: Path) -> None:
    """把 WAL 内容合并回主库文件,保证备份完整。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def clean_database(db_path: Path, *, backup: bool = True) -> dict[str, int]:
    """扫描并重写单个 SQLite 库中的明文密钥,返回 {表名: 更新行数}。

    只更新"脱敏后确实发生变化"的行;库文件不存在返回空 dict。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return {}

    _checkpoint(db_path)
    if backup:
        backup_path = db_path.with_name(db_path.name + BACKUP_SUFFIX)
        shutil.copy2(db_path, backup_path)
        logger.info("clean_secrets: backed up %s -> %s", db_path, backup_path)

    stats: dict[str, int] = {}
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not _is_skipped_table(row[0])
        ]
        rebuilt_fts: set[str] = set()
        for table in tables:
            updated = _clean_table(conn, table)
            if updated:
                stats[table] = updated
                fts_table = f"{table}_fts"
                if fts_table in tables and fts_table not in rebuilt_fts:
                    conn.execute(f"INSERT INTO {fts_table}({fts_table}) VALUES('rebuild')")
                    rebuilt_fts.add(fts_table)
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return stats


def _redact_db_value(value: str) -> str:
    """脱敏单个库字段值:纯文本正则 + JSON 结构遍历双管齐下。

    JSON 形态(如 message_json 列)中字符串带转义引号(``token: \\"xxx\\"``),
    纯文本正则打不穿;解析后由 redact_tool_args 递归处理,既按敏感键名命中
    无特征前缀的随机 token 值,又对其中的纯文本片段(如嵌入的 YAML)脱敏。
    """
    result = redact_text(value)
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return result
    walked = redact_persisted_data(parsed)
    if walked != parsed:
        try:
            result = json.dumps(walked, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    return result


def _clean_table(conn: sqlite3.Connection, table: str) -> int:
    """重写单表中含明文密钥的行,返回更新行数。无 rowid 的表跳过。"""
    try:
        rows = conn.execute(f"SELECT rowid, * FROM {table}").fetchall()
    except sqlite3.Error:
        # WITHOUT ROWID 表或不可读视图等,跳过。
        return 0
    if not rows:
        return 0
    col_names: list[str] = [desc[0] for desc in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
    value_cols = col_names[0:]  # SELECT rowid, * 的列即表自身列
    updated = 0
    for row in rows:
        rowid = row[0]
        new_values: dict[str, str] = {}
        for col, value in zip(value_cols, row[1:]):
            if isinstance(value, str):
                redacted = _redact_db_value(value)
                if redacted != value:
                    new_values[col] = redacted
        if not new_values:
            continue
        set_clause = ", ".join(f'"{col}" = ?' for col in new_values)
        try:
            conn.execute(
                f'UPDATE {table} SET {set_clause} WHERE rowid = ?',
                (*new_values.values(), rowid),
            )
            updated += 1
        except sqlite3.Error as exc:
            logger.warning("clean_secrets: failed to update %s rowid=%s: %s", table, rowid, exc)
    if updated:
        logger.info("clean_secrets: %s updated %d rows", table, updated)
    return updated


def collect_database_paths(config: Any) -> list[Path]:
    """从 AppConfig 收集需要清理的库文件路径(去重、仅保留存在的)。"""
    candidates: list[Path] = []
    memory_dir = getattr(config, "memory_dir", None)
    if memory_dir is not None:
        candidates.append(Path(memory_dir) / "sessions.sqlite3")
    memory_db = getattr(getattr(config, "memory", None), "db_path", None)
    if memory_db:
        candidates.append(Path(memory_db))
    scheduler_db = getattr(getattr(config, "scheduler", None), "db_path", None)
    if scheduler_db:
        candidates.append(Path(scheduler_db))

    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def clean_all_databases(config: Any, *, backup: bool = True) -> dict[str, dict[str, int]]:
    """清理配置涉及的全部 SQLite 库,返回 {库路径: {表名: 更新行数}}。"""
    results: dict[str, dict[str, int]] = {}
    for db_path in collect_database_paths(config):
        stats = clean_database(db_path, backup=backup)
        if stats:
            results[str(db_path)] = stats
    return results
