"""会话渠道归属元数据（sidecar）。

每条会话除了 ``{role_dir}/{session_id}.json`` 持久化的消息历史之外,还会有一份
``{role_dir}/{session_id}.meta.json`` 旁路文件,记录这条会话最初是从哪个渠道写
入的(WebUI / CLI / Telegram / 微信)以及它在该渠道内的 scope_key。

设计要点:
- sidecar 在 ``save_message_history`` 首次写入时一次性落地,后续不再覆盖
  ——一条会话的"渠道归属"只能由首条消息决定,避免跨渠道误改。
- 无 sidecar 的存量会话视为 ``webui``(``DEFAULT_CHANNEL``),不需要迁移脚本。
- ``channel`` 是 ``CHANNEL_CODES`` 中的平台枚举;``scope_key`` 是同一平台内更细
  的隔离键(例如 Telegram 用 ``telegram:{chat_id}:{thread_id}:{user_id}``)。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from openhachimi_agent.core.identifiers import (
    ensure_path_under,
    validate_latest_scope,
    validate_role_name,
    validate_session_id,
)


logger = logging.getLogger(__name__)


# 渠道枚举,与前端 channel filter / channel_context.channel_code 对齐。
# 新增渠道时记得同步更新:
# - ChatRequest.channel(transport/api_models.py)
# - 前端 store/Chat.vue 的下拉选项
CHANNEL_CODES: tuple[str, ...] = ("webui", "cli", "telegram", "weixin")
DEFAULT_CHANNEL: str = "webui"


def is_known_channel(channel: str | None) -> bool:
    return channel in CHANNEL_CODES


def _role_dir(memory_dir: Path, role_name: str) -> Path:
    role = validate_role_name(role_name)
    return ensure_path_under(memory_dir, memory_dir / role, label="角色记忆目录")


def meta_path(memory_dir: Path, role_name: str, session_id: str) -> Path:
    """返回某会话 sidecar 的绝对路径。"""
    safe_session_id = validate_session_id(session_id, allow_legacy=False)
    role_dir = _role_dir(memory_dir, role_name)
    return ensure_path_under(
        memory_dir, role_dir / f"{safe_session_id}.meta.json", label="会话渠道元数据路径"
    )


def load_meta(memory_dir: Path, role_name: str, session_id: str) -> dict | None:
    """读取 sidecar;不存在或损坏时返回 None,调用方按 ``DEFAULT_CHANNEL`` 兜底。"""
    try:
        path = meta_path(memory_dir, role_name, session_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(
            "failed to read session meta role=%s session_id=%s error=%s",
            role_name, session_id, exc,
        )
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "session meta corrupt, fallback to default role=%s session_id=%s error=%s",
            role_name, session_id, exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_meta(
    memory_dir: Path,
    role_name: str,
    session_id: str,
    *,
    channel: str,
    scope_key: str | None = None,
    overwrite: bool = False,
) -> None:
    """写入 sidecar。

    - 默认不覆盖已存在的 sidecar(``overwrite=False``),保证渠道归属由首条消息决定。
    - 不在 ``CHANNEL_CODES`` 中的 ``channel`` 会被强制归到 ``DEFAULT_CHANNEL``,
      但仍会记录原始值到 ``channel_raw`` 字段方便排查。
    - 原子写:先写到同目录临时文件,再 ``os.replace`` 覆盖目标,避免半写。
    """
    if not is_known_channel(channel):
        logger.warning(
            "unknown channel code, fallback to default role=%s session_id=%s channel=%s",
            role_name, session_id, channel,
        )
        channel_raw = channel
        channel = DEFAULT_CHANNEL
    else:
        channel_raw = None

    path = meta_path(memory_dir, role_name, session_id)
    if path.exists() and not overwrite:
        return

    payload: dict[str, object] = {
        "channel": channel,
        "scope_key": validate_latest_scope(scope_key) if scope_key else None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if channel_raw is not None:
        payload["channel_raw"] = channel_raw

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False)
            tmp.flush()
            tmp_name = tmp.name
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        # 仅在 os.replace 失败前(tmp_name 未清空)兜底删临时文件
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def infer_channel(memory_dir: Path, role_name: str, session_id: str) -> str:
    """推断会话渠道:有 sidecar 取其 channel,否则归 ``DEFAULT_CHANNEL``。"""
    meta = load_meta(memory_dir, role_name, session_id)
    if not meta:
        return DEFAULT_CHANNEL
    channel = meta.get("channel")
    if isinstance(channel, str) and is_known_channel(channel):
        return channel
    return DEFAULT_CHANNEL


def is_meta_filename(name: str) -> bool:
    """判定文件名是否为 sidecar(避免被 ``list_sessions`` 当成会话)。"""
    return name.endswith(".meta.json")
