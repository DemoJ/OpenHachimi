"""消息历史持久化。"""

import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage


logger = logging.getLogger(__name__)


def _safe_role_name(role_name: str) -> str:
    return role_name.replace("/", "_").replace("\\", "_")


def get_role_memory_dir(memory_dir: Path, role_name: str) -> Path:
    """返回指定角色对应的持久化消息历史目录。"""
    return memory_dir / _safe_role_name(role_name)


def get_memory_path(memory_dir: Path, role_name: str, session_id: str) -> Path:
    """返回指定角色、指定会话对应的持久化消息历史文件路径。"""
    return get_role_memory_dir(memory_dir, role_name) / f"{session_id}.json"


def get_latest_session_path(memory_dir: Path, role_name: str) -> Path:
    return get_role_memory_dir(memory_dir, role_name) / "latest"


def create_session_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid4().hex[:8]}"


def load_latest_session_id(memory_dir: Path, role_name: str) -> str | None:
    latest_path = get_latest_session_path(memory_dir, role_name)
    if not latest_path.exists():
        legacy_path = memory_dir / f"{_safe_role_name(role_name)}.json"
        if legacy_path.exists():
            return "legacy"
        return None

    session_id = latest_path.read_text(encoding="utf-8").strip()
    return session_id or None


def save_latest_session_id(memory_dir: Path, role_name: str, session_id: str) -> None:
    role_memory_dir = get_role_memory_dir(memory_dir, role_name)
    role_memory_dir.mkdir(parents=True, exist_ok=True)
    get_latest_session_path(memory_dir, role_name).write_text(session_id, encoding="utf-8")


def load_message_history(
    memory_dir: Path,
    role_name: str,
    session_id: str | None = None,
) -> tuple[str, list[ModelMessage]]:
    """从磁盘恢复指定角色的当前会话消息历史。"""
    resolved_session_id = session_id or load_latest_session_id(memory_dir, role_name) or create_session_id()
    if resolved_session_id == "legacy":
        history_path = memory_dir / f"{_safe_role_name(role_name)}.json"
    else:
        history_path = get_memory_path(memory_dir, role_name, resolved_session_id)

    if not history_path.exists():
        logger.info("message history not found role=%s session_id=%s path=%s", role_name, resolved_session_id, history_path)
        return resolved_session_id, []

    raw_json = history_path.read_bytes()
    if not raw_json.strip():
        logger.info("message history empty role=%s session_id=%s path=%s", role_name, resolved_session_id, history_path)
        return resolved_session_id, []

    messages = list(ModelMessagesTypeAdapter.validate_json(raw_json))
    logger.info(
        "message history loaded role=%s session_id=%s messages=%d path=%s",
        role_name,
        resolved_session_id,
        len(messages),
        history_path,
    )
    return resolved_session_id, messages


def save_message_history(memory_dir: Path, role_name: str, session_id: str, history_json: bytes) -> None:
    """将消息历史以 JSON 形式写入磁盘。"""
    role_memory_dir = get_role_memory_dir(memory_dir, role_name)
    role_memory_dir.mkdir(parents=True, exist_ok=True)
    get_memory_path(memory_dir, role_name, session_id).write_bytes(history_json)
    save_latest_session_id(memory_dir, role_name, session_id)
    logger.debug(
        "message history saved role=%s session_id=%s bytes=%d",
        role_name,
        session_id,
        len(history_json),
    )


def start_new_session(memory_dir: Path, role_name: str) -> str:
    """为指定角色创建新会话，并把它设为当前会话。"""
    session_id = create_session_id()
    save_latest_session_id(memory_dir, role_name, session_id)
    logger.info("new memory session created role=%s session_id=%s", role_name, session_id)
    return session_id
