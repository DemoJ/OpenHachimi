"""消息历史持久化。"""

import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage

from openhachimi_agent.core.identifiers import (
    ensure_path_under,
    scope_digest,
    validate_latest_scope,
    validate_role_name,
    validate_session_id,
)


logger = logging.getLogger(__name__)


def _role_name(role_name: str) -> str:
    return validate_role_name(role_name)


def get_role_memory_dir(memory_dir: Path, role_name: str) -> Path:
    """返回指定角色对应的持久化消息历史目录。"""
    return ensure_path_under(memory_dir, memory_dir / _role_name(role_name), label="角色记忆目录")


def get_memory_path(memory_dir: Path, role_name: str, session_id: str) -> Path:
    """返回指定角色、指定会话对应的持久化消息历史文件路径。"""
    safe_session_id = validate_session_id(session_id, allow_legacy=False)
    return ensure_path_under(
        memory_dir,
        get_role_memory_dir(memory_dir, role_name) / f"{safe_session_id}.json",
        label="会话记忆路径",
    )


def get_latest_session_path(memory_dir: Path, role_name: str, latest_scope: str | None = None) -> Path:
    role_dir = get_role_memory_dir(memory_dir, role_name)
    scope = validate_latest_scope(latest_scope)
    if not scope:
        return ensure_path_under(memory_dir, role_dir / "latest", label="latest 会话路径")
    scope_path = role_dir / "latest_by_scope" / scope_digest(scope)
    return ensure_path_under(memory_dir, scope_path, label="scoped latest 会话路径")


def create_session_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid4().hex[:8]}"


def load_latest_session_id(memory_dir: Path, role_name: str, latest_scope: str | None = None) -> str | None:
    latest_path = get_latest_session_path(memory_dir, role_name, latest_scope)
    if not latest_path.exists():
        if latest_scope is None:
            legacy_path = ensure_path_under(memory_dir, memory_dir / f"{_role_name(role_name)}.json", label="legacy 记忆路径")
            if legacy_path.exists():
                return "legacy"
        return None

    session_id = latest_path.read_text(encoding="utf-8").strip()
    if not session_id:
        return None
    try:
        return validate_session_id(session_id, allow_legacy=False)
    except ValueError as exc:
        logger.warning("ignored invalid latest session id role=%s scope=%s path=%s error=%s", role_name, latest_scope, latest_path, exc)
        return None


def save_latest_session_id(memory_dir: Path, role_name: str, session_id: str, latest_scope: str | None = None) -> None:
    safe_session_id = validate_session_id(session_id, allow_legacy=False)
    role_memory_dir = get_role_memory_dir(memory_dir, role_name)
    role_memory_dir.mkdir(parents=True, exist_ok=True)
    latest_path = get_latest_session_path(memory_dir, role_name, latest_scope)
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(safe_session_id, encoding="utf-8")


def load_message_history(
    memory_dir: Path,
    role_name: str,
    session_id: str | None = None,
    latest_scope: str | None = None,
) -> tuple[str, list[ModelMessage]]:
    """从磁盘恢复指定角色的当前会话消息历史。"""
    role = _role_name(role_name)
    if session_id:
        resolved_session_id = validate_session_id(session_id, allow_legacy=True)
    else:
        resolved_session_id = load_latest_session_id(memory_dir, role, latest_scope) or create_session_id()
    if resolved_session_id == "legacy":
        history_path = ensure_path_under(memory_dir, memory_dir / f"{role}.json", label="legacy 记忆路径")
    else:
        history_path = get_memory_path(memory_dir, role, resolved_session_id)

    if not history_path.exists():
        logger.info("message history not found role=%s session_id=%s path=%s", role, resolved_session_id, history_path)
        return resolved_session_id, []

    raw_json = history_path.read_bytes()
    if not raw_json.strip():
        logger.info("message history empty role=%s session_id=%s path=%s", role, resolved_session_id, history_path)
        return resolved_session_id, []

    messages = list(ModelMessagesTypeAdapter.validate_json(raw_json))
    logger.info(
        "message history loaded role=%s session_id=%s messages=%d path=%s",
        role,
        resolved_session_id,
        len(messages),
        history_path,
    )
    return resolved_session_id, messages


def save_message_history(
    memory_dir: Path,
    role_name: str,
    session_id: str,
    history_json: bytes,
    latest_scope: str | None = None,
) -> None:
    """将消息历史以 JSON 形式写入磁盘。"""
    role = _role_name(role_name)
    safe_session_id = validate_session_id(session_id, allow_legacy=False)
    role_memory_dir = get_role_memory_dir(memory_dir, role)
    role_memory_dir.mkdir(parents=True, exist_ok=True)
    get_memory_path(memory_dir, role, safe_session_id).write_bytes(history_json)
    save_latest_session_id(memory_dir, role, safe_session_id, latest_scope)
    logger.debug(
        "message history saved role=%s session_id=%s bytes=%d scope=%s",
        role,
        safe_session_id,
        len(history_json),
        latest_scope,
    )


def list_sessions(memory_dir: Path, role_name: str) -> list[dict]:
    """列举指定角色目录下所有持久化会话文件。

    返回按 mtime 倒序排列的 ``{session_id, mtime, size_bytes}`` 列表，
    跳过 ``latest`` 文件、``latest_by_scope`` 子目录以及无法通过
    ``validate_session_id`` 校验的文件。
    """
    role = _role_name(role_name)
    role_dir = get_role_memory_dir(memory_dir, role)
    if not role_dir.exists():
        return []

    items: list[dict] = []
    for entry in role_dir.iterdir():
        if entry.is_dir():
            continue
        if entry.suffix != ".json":
            continue
        session_id = entry.stem
        try:
            validate_session_id(session_id, allow_legacy=False)
        except ValueError:
            continue
        try:
            stat = entry.stat()
        except OSError as exc:
            logger.warning("skip session due to stat error role=%s path=%s error=%s", role, entry, exc)
            continue
        items.append({
            "session_id": session_id,
            "mtime": stat.st_mtime,
            "size_bytes": stat.st_size,
        })

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def start_new_session(memory_dir: Path, role_name: str, latest_scope: str | None = None) -> str:
    """为指定角色创建新会话，并把它设为当前会话。"""
    role = _role_name(role_name)
    session_id = create_session_id()
    save_latest_session_id(memory_dir, role, session_id, latest_scope)
    logger.info("new memory session created role=%s session_id=%s scope=%s", role, session_id, latest_scope)
    return session_id
