"""长期记忆迁移。

包含:
- :func:`migrate_legacy_histories` — 从 JSON history 文件导入 legacy turn 到 SQLite 存储。
- :func:`run_contamination_cleanup` — 清理 v1 代码中 volatile 前缀和 scheduler payload
  混入的污染 L1 atom。
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from pydantic_ai import ModelMessagesTypeAdapter

from openhachimi_agent.memory.models import MemoryScope, MemoryTurn
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
# legacy history migration
# ---------------------------------------------------------------------------


def migrate_legacy_histories(store: MemoryStore, memory_dir: Path) -> dict[str, int]:
    scanned = 0
    imported = 0
    failed = 0
    seen_turn_ids = _existing_turn_ids(store)
    for history_path in _history_files(memory_dir):
        scanned += 1
        role_name, session_id = _role_and_session(memory_dir, history_path)
        try:
            raw_json = history_path.read_bytes()
            if not raw_json.strip():
                continue
            messages = list(ModelMessagesTypeAdapter.validate_json(raw_json))
        except Exception as exc:
            failed += 1
            logger.warning("legacy memory history migration skipped path=%s error=%s", history_path, exc)
            continue
        for index, pair in enumerate(_message_pairs(messages), start=1):
            user_message, assistant_output = pair
            turn_id = f"legacy:{role_name}:{session_id}:{index}"
            if turn_id in seen_turn_ids:
                continue
            scope = MemoryScope(role_name=role_name, session_id=session_id)
            store.add_turn(
                MemoryTurn(
                    tenant_id=scope.tenant_id,
                    user_id=scope.user_id,
                    role_name=scope.role_name,
                    session_id=scope.session_id,
                    channel=scope.channel,
                    user_message=user_message,
                    assistant_output=assistant_output,
                    turn_id=turn_id,
                    raw_messages_json_ref=str(history_path),
                )
            )
            seen_turn_ids.add(turn_id)
            imported += 1
    return {"scanned": scanned, "imported": imported, "failed": failed}


def _history_files(memory_dir: Path) -> list[Path]:
    if not memory_dir.exists():
        return []
    files: list[Path] = []
    for path in memory_dir.rglob("*.json"):
        if path.name == "latest" or path.name == "long_term_memory.sqlite3":
            continue
        if path.name.endswith(".sqlite3"):
            continue
        files.append(path)
    return sorted(files)


def _role_and_session(memory_dir: Path, history_path: Path) -> tuple[str, str]:
    relative = history_path.relative_to(memory_dir)
    if len(relative.parts) == 1:
        return history_path.stem, "legacy"
    return relative.parts[0], history_path.stem


def _message_pairs(messages: list[Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pending_user = ""
    for message in messages:
        role = _message_role(message)
        text = _message_text(message)
        if not text:
            continue
        if role == "user":
            pending_user = text
        elif role in {"assistant", "model"} and pending_user:
            pairs.append((pending_user, text))
            pending_user = ""
    return pairs


def _message_role(message: Any) -> str:
    kind = str(getattr(message, "kind", "")).lower()
    if kind == "request":
        return "user"
    if kind == "response":
        return "assistant"
    role = str(getattr(message, "role", "")).lower()
    return role


def _message_text(message: Any) -> str:
    parts = getattr(message, "parts", []) or []
    texts: list[str] = []
    for part in parts:
        content = getattr(part, "content", None)
        if content is None:
            content = getattr(part, "text", None)
        if content:
            texts.append(str(content))
    if texts:
        return "\n".join(texts).strip()
    content = getattr(message, "content", None)
    return str(content).strip() if content else ""


def _existing_turn_ids(store: MemoryStore) -> set[str]:
    with store.connect() as conn:
        rows = conn.execute("SELECT turn_id FROM memory_turns WHERE turn_id LIKE 'legacy:%'").fetchall()
    return {str(row["turn_id"]) for row in rows}


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