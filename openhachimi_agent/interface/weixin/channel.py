"""微信 iLink 协议的原生渠道接入。"""

import asyncio
import json
import logging
import mimetypes
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.interface.weixin.ilink_client import TYPING_STATUS_CANCEL, TYPING_STATUS_START, WeixinClient
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.storage.attachments import AttachmentError, AttachmentStorage
from openhachimi_agent.transport.api_models import ArtifactRef, AttachmentRef

logger = logging.getLogger(__name__)

# 微信账号凭证文件的相对路径名（相对于 config.base_dir）
_ACCOUNT_REL_PATH = Path(".memory") / "weixin_account.json"
_ACCOUNT_WATCH_INTERVAL_SECONDS = 5.0
_MEDIA_BATCH_DELAY_SECONDS = 3.0
_RECENT_MEDIA_TTL_SECONDS = 10 * 60.0
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_VIDEO = 4
ITEM_FILE = 5
_MEDIA_KIND_BY_TYPE = {
    ITEM_IMAGE: "image",
    ITEM_VIDEO: "video",
    ITEM_FILE: "file",
}
_MEDIA_LABELS = {
    "image": "图片",
    "video": "视频",
    "file": "文件",
}
_IMAGE_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"BM", ".bmp", "image/bmp"),
    (b"RIFF", ".webp", "image/webp"),
)
_RECENT_IMAGE_REFERENCE_KEYWORDS = (
    "图",
    "图片",
    "照片",
    "截图",
    "这张",
    "这个",
    "这里",
    "上面",
    "里面",
    "看下",
    "看看",
    "识别",
    "提取",
    "文字",
    "表格",
    "二维码",
    "image",
    "photo",
    "picture",
    "screenshot",
)


@dataclass
class _PreparedWeixinMessage:
    from_user: str
    to_user: str
    session_key: str
    scope_key: str
    context_token: str
    text_content: str
    attachments: list[AttachmentRef] = field(default_factory=list)
    media_hints: list[str] = field(default_factory=list)


@dataclass
class _RecentMediaEntry:
    attachment: AttachmentRef
    created_at: float


def _account_file(config: AppConfig) -> Path:
    """基于项目根目录返回微信账号凭证文件的绝对路径。"""
    return config.base_dir / _ACCOUNT_REL_PATH


def _account_signature(path: Path) -> tuple[int, int] | None:
    try:
        if not path.is_file():
            return None
        stat = path.stat()
    except OSError as exc:
        logger.debug("检查微信账号文件失败：%s", exc)
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是有效数字，使用默认值 %.1f", name, raw, default)
        return default
    return max(0.0, value)


def _extract_text_content(items: list[Dict[str, Any]]) -> str:
    """从 iLink item_list 中提取可交给 Agent 的文本内容。"""
    parts: list[str] = []
    for item in items:
        item_type = item.get("type")
        if item_type == ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text", "")
            if text:
                parts.append(str(text))
        elif item_type == ITEM_VOICE:
            voice_text = (item.get("voice_item") or {}).get("text", "")
            if voice_text:
                parts.append(f"用户发送了一条微信语音消息，语音转写内容：{voice_text}")
    return "\n".join(part.strip() for part in parts if part and part.strip()).strip()


def _message_session_keys(msg: Dict[str, Any], from_user: str) -> tuple[str, str, str]:
    group_id = msg.get("group_id", "")
    session_key = group_id if group_id else from_user
    safe_session_key = session_key.replace("@", "_at_").replace("-", "_")
    return session_key, f"wx_{safe_session_key}", group_id if group_id else from_user


def _has_image_attachment(attachments: list[AttachmentRef]) -> bool:
    return any(attachment.kind == "image" for attachment in attachments)


def _text_mentions_recent_media(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return any(keyword in normalized for keyword in _RECENT_IMAGE_REFERENCE_KEYWORDS)


def _walk_values(obj: Any):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_values(value)


def _first_value_by_keys(obj: Any, keys: tuple[str, ...]) -> str:
    wanted = {key.lower() for key in keys}
    for key, value in _walk_values(obj):
        if str(key).lower() in wanted and value not in (None, ""):
            return str(value)
    return ""


def _first_url(obj: Any) -> str:
    for _, value in _walk_values(obj):
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


def _media_item_key(kind: str) -> str:
    return {
        "image": "image_item",
        "video": "video_item",
        "file": "file_item",
    }.get(kind, "")


def _media_reference(item: Dict[str, Any], kind: str) -> Dict[str, Any]:
    key = _media_item_key(kind)
    if not key:
        return {}
    media_item = item.get(key) or {}
    media = media_item.get("media") or {}
    return media if isinstance(media, dict) else {}


def _media_kind(item: Dict[str, Any]) -> str | None:
    if "image_item" in item:
        return "image"
    if "file_item" in item:
        return "file"
    if "video_item" in item:
        return "video"
    return _MEDIA_KIND_BY_TYPE.get(item.get("type"))


def _media_aes_key(item: Dict[str, Any], kind: str) -> str:
    media = _media_reference(item, kind)
    media_key = str(media.get("aes_key") or "").strip()
    if media_key:
        return media_key
    key = _media_item_key(kind)
    media_item = item.get(key) or {}
    aeskey = str(media_item.get("aeskey") or "").strip()
    return aeskey


def _media_full_url(item: Dict[str, Any], kind: str) -> str:
    media = _media_reference(item, kind)
    return str(media.get("full_url") or "").strip()


def _media_encrypt_query_param(item: Dict[str, Any], kind: str) -> str:
    media = _media_reference(item, kind)
    return str(media.get("encrypt_query_param") or "").strip()


def _media_download_url(item: Dict[str, Any], kind: str) -> str:
    full_url = _media_full_url(item, kind)
    if full_url:
        return full_url
    return _first_url(item)


def _media_name(item: Dict[str, Any], kind: str, url: str) -> str:
    name = _first_value_by_keys(item, ("name", "filename", "file_name", "title"))
    if name:
        return name
    if url:
        tail = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        if tail:
            return tail
    return f"wechat-{kind}"


def _media_content_type(item: Dict[str, Any], kind: str, url: str) -> str:
    content_type = _first_value_by_keys(item, ("mime_type", "mimeType", "content_type", "contentType"))
    if not content_type:
        content_type = mimetypes.guess_type(url or _media_name(item, kind, url))[0] or {
            "image": "image/jpeg",
            "video": "video/mp4",
            "file": "application/octet-stream",
        }.get(kind, "application/octet-stream")
    if kind == "file" and content_type.startswith("video/"):
        return "application/octet-stream"
    return content_type


def _media_size(item: Dict[str, Any]) -> int | None:
    size = _first_value_by_keys(item, ("size", "file_size", "fileSize"))
    if size.isdigit():
        return int(size)
    return None


def _detect_image_type(data: bytes) -> tuple[str, str] | None:
    for signature, suffix, content_type in _IMAGE_SIGNATURES:
        if signature == b"RIFF":
            if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
                return suffix, content_type
            continue
        if data.startswith(signature):
            return suffix, content_type
    return None


def _ensure_supported_image(data: bytes) -> tuple[str, str]:
    detected = _detect_image_type(data)
    if detected is None:
        snippet = data[:80].decode("utf-8", errors="replace")
        raise ValueError(f"下载结果不是支持的图片格式，开头内容：{snippet!r}")
    return detected


def _format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown size"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _format_artifact_notice(artifacts: list[ArtifactRef]) -> str:
    if not artifacts:
        return ""
    lines = ["微信渠道暂不能直接上传生成文件，已生成以下文件："]
    for artifact in artifacts:
        detail = f"- {artifact.filename} ({_format_size(artifact.size_bytes)})：{artifact.local_path}"
        if artifact.download_url:
            detail += f"；HTTP 下载路径：{artifact.download_url}"
        if artifact.description:
            detail += f"；{artifact.description}"
        lines.append(detail)
    return "\n".join(lines)


class WeixinChannel:
    def __init__(self, service: AgentService, config: AppConfig):
        self.service = service
        self.config = config
        self.client = WeixinClient()
        self.sync_buf = ""
        self.account_id = ""
        self.attachment_storage = AttachmentStorage(
            config.attachments_dir,
            config.max_attachment_size_bytes,
            config.allowed_attachment_mime_types,
            config.base_dir,
        )
        self.media_batch_delay_seconds = _env_float(
            "OPENHACHIMI_WEIXIN_MEDIA_BATCH_DELAY_SECONDS",
            _MEDIA_BATCH_DELAY_SECONDS,
        )
        self.recent_media_ttl_seconds = _env_float(
            "OPENHACHIMI_WEIXIN_RECENT_MEDIA_TTL_SECONDS",
            _RECENT_MEDIA_TTL_SECONDS,
        )
        self.recent_media_max_attachments = max(1, config.vision.max_images_per_message)
        self._pending_media_messages: dict[str, _PreparedWeixinMessage] = {}
        self._pending_media_tasks: dict[str, asyncio.Task] = {}
        self._recent_media: dict[str, list[_RecentMediaEntry]] = {}
        self._media_context_lock = asyncio.Lock()

    @property
    def account_path(self) -> Path:
        return _account_file(self.config)

    def _ensure_media_context_state(self) -> None:
        if not hasattr(self, "media_batch_delay_seconds"):
            self.media_batch_delay_seconds = _MEDIA_BATCH_DELAY_SECONDS
        if not hasattr(self, "recent_media_ttl_seconds"):
            self.recent_media_ttl_seconds = _RECENT_MEDIA_TTL_SECONDS
        if not hasattr(self, "recent_media_max_attachments"):
            self.recent_media_max_attachments = max(1, self.config.vision.max_images_per_message)
        if not hasattr(self, "_pending_media_messages"):
            self._pending_media_messages = {}
        if not hasattr(self, "_pending_media_tasks"):
            self._pending_media_tasks = {}
        if not hasattr(self, "_recent_media"):
            self._recent_media = {}
        if not hasattr(self, "_media_context_lock"):
            self._media_context_lock = asyncio.Lock()

    async def _download_media_attachments(
        self,
        items: list[Dict[str, Any]],
        namespace: str,
    ) -> tuple[list[AttachmentRef], list[str]]:
        attachments: list[AttachmentRef] = []
        hints: list[str] = []

        for item in items:
            kind = _media_kind(item)
            if not kind:
                continue
            label = _MEDIA_LABELS.get(kind, "媒体")
            url = _media_download_url(item, kind)
            encrypted_query_param = _media_encrypt_query_param(item, kind)
            aes_key = _media_aes_key(item, kind)
            filename = _media_name(item, kind, url)
            content_type = _media_content_type(item, kind, url)
            declared_size = _media_size(item)
            metadata: dict[str, object] = {
                "weixin_item_type": item.get("type"),
                "weixin_media_kind": kind,
            }
            if url:
                metadata["weixin_media_url"] = url
            if encrypted_query_param:
                metadata["weixin_encrypt_query_param"] = encrypted_query_param

            try:
                self.attachment_storage.validate_metadata(
                    filename=filename,
                    content_type=content_type,
                    size_bytes=declared_size,
                )
            except AttachmentError as exc:
                hints.append(f"用户发送了微信{label}“{filename}”，但附件无法处理：{exc}")
                continue

            if not encrypted_query_param and not url:
                hints.append(f"用户发送了微信{label}“{filename}”，但消息中没有可下载链接。")
                continue

            try:
                if encrypted_query_param or _media_full_url(item, kind):
                    data, downloaded_content_type = await self.client.download_encrypted_media(
                        encrypted_query_param=encrypted_query_param or None,
                        aes_key=aes_key or None,
                        full_url=_media_full_url(item, kind) or None,
                        max_size_bytes=self.config.max_attachment_size_bytes,
                    )
                else:
                    data, downloaded_content_type = await self.client.download_media(
                        url,
                        self.config.max_attachment_size_bytes,
                    )
                if kind == "image":
                    suffix, detected_content_type = _ensure_supported_image(data)
                    content_type = detected_content_type
                    if not Path(filename).suffix:
                        filename = f"{filename}{suffix}"
                if (
                    downloaded_content_type
                    and kind != "image"
                    and not (kind == "file" and downloaded_content_type.startswith("video/"))
                ):
                    content_type = downloaded_content_type
                target = self.attachment_storage.build_path(
                    source="weixin",
                    namespace=namespace,
                    filename=filename,
                    content_type=content_type,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                ref = self.attachment_storage.to_ref(
                    path=target,
                    source="weixin",
                    filename=target.name,
                    content_type=content_type,
                    size_bytes=len(data),
                    metadata=metadata,
                )
                attachments.append(ref)
                hints.append(f"用户发送了微信{label}“{ref.filename}”，已作为附件接收。")
            except Exception as exc:
                logger.warning("微信%s下载失败 url=%s encrypted=%s: %s", label, url[:120], bool(encrypted_query_param), exc)
                hints.append(f"用户发送了微信{label}“{filename}”，但附件下载失败：{exc}")

        return attachments, hints

    def _format_message_text(self, prepared: _PreparedWeixinMessage) -> str:
        text_content = prepared.text_content.strip()
        if not text_content and prepared.attachments:
            text_content = "用户发送了微信媒体消息，请根据附件内容协助处理。"
        if prepared.media_hints:
            text_content = "\n".join(part for part in [text_content, *prepared.media_hints] if part).strip()
        return text_content

    def _prune_recent_media(self, session_key: str, now: float | None = None) -> list[_RecentMediaEntry]:
        self._ensure_media_context_state()
        now = time.monotonic() if now is None else now
        entries = [
            entry
            for entry in self._recent_media.get(session_key, [])
            if now - entry.created_at <= self.recent_media_ttl_seconds
        ]
        self._recent_media[session_key] = entries[-self.recent_media_max_attachments :]
        return self._recent_media[session_key]

    def _record_recent_media(self, session_key: str, attachments: list[AttachmentRef]) -> None:
        self._ensure_media_context_state()
        images = [attachment for attachment in attachments if attachment.kind == "image"]
        if not images:
            return
        now = time.monotonic()
        entries = self._prune_recent_media(session_key, now)
        seen_paths = {entry.attachment.local_path for entry in entries}
        for attachment in images:
            if attachment.local_path in seen_paths:
                continue
            entries.append(_RecentMediaEntry(attachment=attachment, created_at=now))
        self._recent_media[session_key] = entries[-self.recent_media_max_attachments :]

    def _recent_media_attachments(self, session_key: str) -> list[AttachmentRef]:
        return [entry.attachment for entry in self._prune_recent_media(session_key)]

    async def _pop_pending_media_message(self, session_key: str) -> _PreparedWeixinMessage | None:
        self._ensure_media_context_state()
        async with self._media_context_lock:
            task = self._pending_media_tasks.pop(session_key, None)
            if task and not task.done():
                task.cancel()
            return self._pending_media_messages.pop(session_key, None)

    async def _queue_pending_media_message(self, prepared: _PreparedWeixinMessage) -> None:
        self._ensure_media_context_state()
        if self.media_batch_delay_seconds <= 0:
            await self._process_prepared_message(prepared)
            return

        async with self._media_context_lock:
            existing = self._pending_media_messages.get(prepared.session_key)
            if existing is None:
                self._pending_media_messages[prepared.session_key] = prepared
            else:
                existing.attachments.extend(prepared.attachments)
                existing.media_hints.extend(prepared.media_hints)
                existing.context_token = prepared.context_token or existing.context_token
                existing.to_user = prepared.to_user
                prepared = existing

            task = self._pending_media_tasks.pop(prepared.session_key, None)
            if task and not task.done():
                task.cancel()
            self._pending_media_tasks[prepared.session_key] = asyncio.create_task(
                self._flush_pending_media_message(prepared.session_key)
            )

    async def _flush_pending_media_message(self, session_key: str) -> None:
        self._ensure_media_context_state()
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self.media_batch_delay_seconds)
            async with self._media_context_lock:
                if self._pending_media_tasks.get(session_key) is not current_task:
                    return
                prepared = self._pending_media_messages.pop(session_key, None)
                self._pending_media_tasks.pop(session_key, None)
            if prepared is not None:
                await self._process_prepared_message(prepared)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("处理延迟微信媒体消息失败 session=%s", session_key)

    async def _cancel_pending_media_tasks(self) -> None:
        self._ensure_media_context_state()
        tasks = list(self._pending_media_tasks.values())
        self._pending_media_tasks.clear()
        self._pending_media_messages.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def _load_account(self) -> bool:
        path = self.account_path
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
                self.client.token = data.get("token")
                self.account_id = data.get("account_id", "")
                if self.client.token:
                    logger.info("已加载微信凭证：%s", path)
                    return True
                else:
                    logger.warning("微信凭证文件存在但缺少 token：%s", path)
            except Exception as e:
                logger.warning("加载微信账号失败：%s", e)
        else:
            logger.warning("微信凭证文件不存在：%s", path)
        return False

    async def _process_prepared_message(self, prepared: _PreparedWeixinMessage) -> None:
        text_content = self._format_message_text(prepared)
        if not text_content.strip():
            logger.debug("跳过空消息")
            return

        self._record_recent_media(prepared.session_key, prepared.attachments)

        channel_context = {
            "type": "weixin",
            "platform": "weixin",
            "channel_code": "weixin",
            "session_scope_key": prepared.scope_key,
        }

        # 优先命令分派:命中即直接回复并返回,不进 LLM、不写入对话历史。
        # 微信只在用户消息没有附件时尝试,避免媒体场景下误识别。
        if not prepared.attachments:
            dispatch = getattr(self.service, "dispatch_command", None)
            outcome = None
            if dispatch is not None:
                outcome = await dispatch(
                    prepared.text_content,
                    channel_context=channel_context,
                    channel="weixin",
                )
            if outcome is not None:
                reply = outcome.message or "已完成。"
                client_id = f"openhachimi-{uuid.uuid4().hex[:8]}"
                await self.client.send_message(
                    to_user_id=prepared.to_user,
                    text=reply,
                    context_token=prepared.context_token,
                    client_id=client_id,
                )
                logger.info(
                    "微信命令已分派 来自 %s kind=%s text=%s",
                    prepared.from_user,
                    outcome.kind,
                    prepared.text_content[:30],
                )
                return

        logger.info(
            "收到微信消息 来自 %s: %s attachment_count=%d",
            prepared.from_user,
            text_content[:50],
            len(prepared.attachments),
        )

        typing_ticket = await self.client.get_typing_ticket(prepared.to_user)
        typing_task = None
        if typing_ticket:
            try:
                await self.client.send_typing(prepared.to_user, typing_ticket, TYPING_STATUS_START)

                async def _keep_typing():
                    while True:
                        await asyncio.sleep(5)
                        try:
                            await self.client.send_typing(prepared.to_user, typing_ticket, TYPING_STATUS_START)
                        except Exception as e:
                            logger.debug("刷新 typing 状态失败: %s", e)
                            break

                typing_task = asyncio.create_task(_keep_typing())
                logger.debug("已启动 typing 指示器 for %s", prepared.to_user)
            except Exception as e:
                logger.debug("启动 typing 指示器失败: %s", e)

        try:
            response = await self.service.send_message(
                message=text_content,
                role=self.config.default_role_name,
                session_id=None,
                attachments=prepared.attachments,
                channel_context=channel_context,
                channel="weixin",
            )
        finally:
            if typing_task:
                typing_task.cancel()
                with suppress(asyncio.CancelledError):
                    await typing_task
            if typing_ticket:
                try:
                    await self.client.send_typing(prepared.to_user, typing_ticket, TYPING_STATUS_CANCEL)
                    logger.debug("已取消 typing 指示器 for %s", prepared.to_user)
                except Exception as e:
                    logger.debug("取消 typing 指示器失败: %s", e)

        client_id = f"openhachimi-{uuid.uuid4().hex[:8]}"
        artifact_notice = _format_artifact_notice(response.artifacts)
        reply_text = response.output
        if artifact_notice:
            reply_text = "\n\n".join(part for part in [reply_text, artifact_notice] if part.strip())
        if not reply_text.strip():
            reply_text = "已完成。"
        await self.client.send_message(
            to_user_id=prepared.to_user,
            text=reply_text,
            context_token=prepared.context_token,
            client_id=client_id,
        )
        logger.info("已回复微信消息给 %s", prepared.to_user)

    async def _handle_message(self, msg: Dict[str, Any]):
        try:
            # 只处理入站消息（message_type == 1），避免处理自己发出的消息
            message_type = msg.get("message_type")
            if message_type != 1:
                logger.debug("跳过非入站消息 message_type=%s", message_type)
                return

            from_user = msg.get("from_user_id", "")
            if not from_user:
                logger.warning("消息缺少 from_user_id")
                return

            # 提取文本内容。语音消息使用 iLink 自带的 voice_item.text 转写。
            items = msg.get("item_list", [])
            text_content = _extract_text_content(items)

            message_id = str(
                msg.get("message_id")
                or msg.get("msg_id")
                or msg.get("server_msg_id")
                or uuid.uuid4().hex[:8]
            )
            attachments, media_hints = await self._download_media_attachments(
                items,
                namespace=f"{from_user}_{message_id}",
            )

            session_key, scope_key, to_user = _message_session_keys(msg, from_user)
            context_token = msg.get("context_token", "")
            prepared = _PreparedWeixinMessage(
                from_user=from_user,
                to_user=to_user,
                session_key=session_key,
                scope_key=scope_key,
                context_token=context_token,
                text_content=text_content,
                attachments=attachments,
                media_hints=media_hints,
            )

            has_user_text = bool(text_content.strip())
            if has_user_text:
                pending = await self._pop_pending_media_message(session_key)
                if pending is not None:
                    pending.text_content = text_content
                    pending.attachments.extend(attachments)
                    pending.media_hints.extend(media_hints)
                    pending.context_token = context_token or pending.context_token
                    pending.to_user = to_user
                    prepared = pending
                    logger.info("已将微信后续文字与短时间内的图片消息合并处理 session=%s", scope_key)
                elif not attachments and _text_mentions_recent_media(text_content):
                    recent_attachments = self._recent_media_attachments(session_key)
                    if recent_attachments:
                        prepared.attachments.extend(recent_attachments)
                        prepared.media_hints.append("用户最近发送过微信图片，已将最近的图片作为本条消息的上下文。")
                        logger.info("已为微信文字消息附加近期图片上下文 session=%s count=%d", scope_key, len(recent_attachments))

            if not has_user_text and _has_image_attachment(prepared.attachments):
                await self._queue_pending_media_message(prepared)
                return

            await self._process_prepared_message(prepared)
        except Exception as e:
            logger.exception("处理微信消息时出错：%s", msg)

    async def run_loop(self):
        if not await self._load_account():
            logger.warning("微信 token 缺失，请运行 `hachimi weixin` 登录。微信渠道将保持未激活状态。")
            return

        if not self.client.token:
            return

        logger.info("微信渠道轮询循环已启动，正在监听消息...")
        error_count = 0

        while True:
            try:
                updates = await self.client.get_updates(self.sync_buf)
                ret = updates.get("ret")
                errcode = updates.get("errcode")

                # 会话过期：部分 iLink 响应会带 ret/errmsg，正常 get_updates 也可能不带 ret。
                if ret in (-14, -2) and (updates.get("errmsg", "").lower() == "unknown error" or ret == -14):
                    logger.warning("微信会话已过期，请运行 `hachimi weixin` 重新登录。")
                    path = self.account_path
                    if path.exists():
                        path.unlink()
                    self.client.token = None
                    break

                # 成功条件：ret 为 0 或 None，且 errcode 为 0 或 None
                if ret not in (0, None) or errcode not in (0, None):
                    logger.error("微信 get_updates 错误：%s", updates)
                    error_count += 1
                    await asyncio.sleep(min(30, error_count * 2))
                    continue

                error_count = 0
                # 只使用 get_updates_buf 作为游标
                if updates.get("get_updates_buf"):
                    self.sync_buf = updates["get_updates_buf"]

                msgs = updates.get("msgs", [])
                for m in msgs:
                    # 不阻塞主轮询
                    asyncio.create_task(self._handle_message(m))

            except Exception as e:
                logger.error("微信轮询异常：%s", e)
                error_count += 1
                await asyncio.sleep(min(30, error_count * 2))


async def _stop_channel_task(channel_task: asyncio.Task | None, channel: WeixinChannel | None) -> None:
    if channel_task is not None:
        if not channel_task.done():
            channel_task.cancel()
        try:
            await channel_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("微信渠道任务异常退出")
    if channel is not None:
        with suppress(Exception):
            await channel._cancel_pending_media_tasks()
        with suppress(Exception):
            await channel.client.close()


async def _weixin_channel_supervisor(
    service: AgentService,
    config: AppConfig,
    poll_interval: float = _ACCOUNT_WATCH_INTERVAL_SECONDS,
) -> None:
    account_path = _account_file(config)
    channel: WeixinChannel | None = None
    channel_task: asyncio.Task | None = None
    active_signature: tuple[int, int] | None = None
    failed_signature: tuple[int, int] | None = None
    missing_logged = False

    try:
        while True:
            signature = _account_signature(account_path)

            if channel_task is not None and channel_task.done():
                await _stop_channel_task(channel_task, channel)
                if signature is not None and signature == active_signature:
                    failed_signature = signature
                channel = None
                channel_task = None
                active_signature = None

            if signature is None:
                if channel_task is not None:
                    logger.info("微信账号文件已移除，正在停止微信渠道：%s", account_path)
                    await _stop_channel_task(channel_task, channel)
                    channel = None
                    channel_task = None
                    active_signature = None
                    failed_signature = None
                if not missing_logged:
                    logger.info(
                        "微信账号文件不存在 (%s)，微信渠道暂未启动；服务将持续监听登录状态。",
                        account_path,
                    )
                    missing_logged = True
            else:
                missing_logged = False
                if channel_task is not None and signature != active_signature:
                    logger.info("检测到微信账号文件更新，正在重启微信渠道：%s", account_path)
                    await _stop_channel_task(channel_task, channel)
                    channel = None
                    channel_task = None
                    active_signature = None
                    failed_signature = None

                if channel_task is None and signature != failed_signature:
                    logger.info("检测到微信账号文件 (%s)，正在启动微信渠道...", account_path)
                    channel = WeixinChannel(service, config)
                    channel_task = asyncio.create_task(channel.run_loop())
                    active_signature = signature

            await asyncio.sleep(poll_interval)
    finally:
        await _stop_channel_task(channel_task, channel)


@asynccontextmanager
async def weixin_lifespan(app):
    config: AppConfig = app.state.config
    service: AgentService = app.state.service

    supervisor_task = asyncio.create_task(_weixin_channel_supervisor(service, config))

    yield

    supervisor_task.cancel()
    with suppress(asyncio.CancelledError):
        await supervisor_task
