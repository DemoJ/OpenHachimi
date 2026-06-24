"""长期记忆维护。

包含:
- :func:`run_contamination_cleanup` — 清理 v1 代码中 volatile 前缀和 scheduler payload
  混入的污染 L1 atom。

历史上还有一个 ``migrate_legacy_histories`` 函数,用于把旧版会话 JSON 文件里的
turn 导入 SQLite 长期记忆库。会话存储已经从 JSON 整体迁到 SQLite
(``storage/session_store.py``),旧 JSON 文件由用户手工清理,这个迁移工具一同
退役。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from openhachimi_agent.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# v1 污染标记:content 含这些子串的 L1 atom,将被视为污染数据清理
_CONTAMINATION_MARKERS = (
    "[系统环境] 当前真实时间:",
    "以下是长期记忆召回结果",
    "<memory-context>",
    '<memory id="',
    "<skill name=",
    "Skill Metadata]",
    "[Path Note]",
    "相对路径仍相对于当前项目工作区根目录",
    "请执行以下用户任务。必须遵守 TaskFrame",
    "你正在执行一个已经到期的定时任务",
    "定时任务 ID：",
)


# ---------------------------------------------------------------------------
# v1 contamination cleanup
# ---------------------------------------------------------------------------


def _find_contaminated(store: MemoryStore, *, limit: int = 5000) -> list[dict[str, Any]]:
    """查找 content 含已知污染标记的活跃 L1 atom。"""
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, content, memory_type, confidence, stability, created_at, updated_at, status
            FROM memory_atoms
            WHERE status = 'active'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    bad: list[dict[str, Any]] = []
    for row in rows:
        content: str = row["content"] or ""
        lowered = content.lower()
        if any(marker.lower() in lowered for marker in _CONTAMINATION_MARKERS):
            bad.append({
                "id": row["id"],
                "content_preview": content[:200],
                "memory_type": row["memory_type"],
                "confidence": row["confidence"],
                "stability": row["stability"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "status": row["status"],
            })
    return bad


def _expire_atoms(store: MemoryStore, atom_ids: list[str]) -> int:
    """将指定 atom 标记为 expired 并从 FTS 移除。"""
    if not atom_ids:
        return 0
    with store.connect() as conn:
        now = "2026-06-23T00:00:00+00:00"
        placeholders = ",".join("?" for _ in atom_ids)
        conn.execute(
            f"UPDATE memory_atoms SET status = 'expired', updated_at = ? WHERE id IN ({placeholders})",
            (now, *atom_ids),
        )
        conn.execute(
            f"DELETE FROM memory_atoms_fts WHERE id IN ({placeholders})",
            atom_ids,
        )
    return len(atom_ids)


def run_contamination_cleanup(
    db_path: Path,
    *,
    dry_run: bool = True,
    limit: int = 5000,
) -> dict[str, Any]:
    """扫描并清理长期记忆库中的污染 L1 atom。

    Args:
        db_path: SQLite 数据库路径。
        dry_run: True(默认)时只输出报告,不实际修改。
        limit: 最多检查多少条活跃 atom。

    Returns:
        {"checked": int, "contaminated": int, "expired": int}
    """
    # Windows GBK 终端兼容
    _print = lambda x: sys.stdout.buffer.write((x + "\n").encode("utf-8", errors="replace")) is None or sys.stdout.flush()

    store = MemoryStore(db_path)
    contaminated = _find_contaminated(store, limit=limit)

    checked = min(limit, 5000)
    cnt = len(contaminated)
    expired = 0

    _print(f"【存量记忆污染清理】{db_path}")
    _print(f"  检查数量上限: {limit}")
    _print(f"  发现污染原子: {cnt}")
    _print("")

    if not cnt:
        _print("未发现污染原子,无需清理。")
        return {"checked": checked, "contaminated": 0, "expired": 0}

    _print("污染原子样本(最多 10 条):")
    for i, atom in enumerate(contaminated[:10], 1):
        _print(f"  {i}. [{atom['memory_type']}] {atom['content_preview'][:120]}")
    _print("")

    if dry_run:
        _print("dry-run 模式,未实际修改。传递 apply=True 执行清理。")
    else:
        atom_ids = [a["id"] for a in contaminated]
        expired = _expire_atoms(store, atom_ids)
        _print(f"已标记 {expired} 条 atom 为 expired。")

    return {"checked": checked, "contaminated": cnt, "expired": expired}
