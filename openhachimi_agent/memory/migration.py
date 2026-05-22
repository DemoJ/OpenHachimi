"""长期记忆迁移。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic_ai import ModelMessagesTypeAdapter

from openhachimi_agent.memory.models import MemoryScope, MemoryTurn
from openhachimi_agent.memory.store import MemoryStore

logger = logging.getLogger(__name__)


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
