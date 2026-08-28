"""微信 iLink 协议的原生渠道接入。"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.interface.presenter import ToolProgressPresenter
from openhachimi_agent.interface.weixin import media as weixin_media
from openhachimi_agent.interface.weixin.ilink_client import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_TEXT,
    ITEM_VIDEO,
    ITEM_VOICE,
    TYPING_STATUS_CANCEL,
    TYPING_STATUS_START,
    WeixinClient,
)
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.service.agent_runtime.streaming import StreamEventItem
from openhachimi_agent.storage.attachments import AttachmentError, AttachmentStorage
from openhachimi_agent.transport.api_models import ArtifactRef, AttachmentRef

logger = logging.getLogger(__name__)

# 微信账号凭证文件的相对路径名（相对于 config.base_dir）
_ACCOUNT_REL_PATH = Path(".memory") / "weixin_account.json"
_ACCOUNT_WATCH_INTERVAL_SECONDS = 5.0
_MEDIA_BATCH_DELAY_SECONDS = 3.0
_RECENT_MEDIA_TTL_SECONDS = 10 * 60.0
# 对齐 hermes gateway/platforms/weixin.py: Hermes 微信实测限频点在连续发送后,
# 通过单条 2000 字符 + 1.5s 间隔 + 4 次重试 + 熔断窗口 来规避 10 条后静默。
_WEIXIN_MAX_MSG_CHARS = 2000  # hermes WeixinAdapter.MAX_MESSAGE_LENGTH
_WEIXIN_SEND_MIN_INTERVAL = 1.5  # hermes send_chunk_delay_seconds 1.5
_WEIXIN_SEND_MAX_RETRIES = 4  # hermes send_chunk_retries 4
_WEIXIN_RATE_LIMIT_ERRCODE = -2
_WEIXIN_SESSION_EXPIRED_ERRCODE = -14
_WEIXIN_RATE_LIMIT_CIRCUIT_THRESHOLD = 1  # 30s 内 1 次限频即熔断
_WEIXIN_RATE_LIMIT_CIRCUIT_WINDOW = 30.0
_WEIXIN_RATE_LIMIT_CIRCUIT_OPEN = 30.0
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
    # 群聊会话按"群+发送者"隔离:全群共享一个会话会导致成员上下文互相污染、
    # 危险命令确认被任意成员答复(与 Telegram 渠道的按用户隔离语义对齐)。
    # 回复目标(to_user)仍是群,即回复发到群里。
    if group_id:
        session_key = f"{group_id}:{from_user}"
    else:
        session_key = from_user
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
    lines = ["以下生成文件未能通过微信发送，可从本地路径获取："]
    for artifact in artifacts:
        detail = f"- {artifact.filename} ({_format_size(artifact.size_bytes)})：{artifact.local_path}"
        if artifact.download_url:
            detail += f"；HTTP 下载路径：{artifact.download_url}"
        if artifact.description:
            detail += f"；{artifact.description}"
        lines.append(detail)
    return "\n".join(lines)


_MD_STRUCTURAL_LINE_RE = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\||```)")


def _is_md_structural_line(line: str) -> bool:
    return bool(_MD_STRUCTURAL_LINE_RE.match(line.lstrip()))


def _ensure_weixin_line_breaks(text: str) -> str:
    """iLink 微信客户端按 Markdown 渲染文本消息：单个换行会被折叠成空格，
    只有空行才产生换行。把相邻普通文本行之间的单换行升级为空行，保证逐行
    显示；代码块内部保持原样，列表/标题/表格/引用等 Markdown 结构行之间的
    单换行本身语义正确，也不做改写。"""
    lines = text.splitlines()
    out: list[str] = []
    in_fence = False
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        out.append(line)
        if in_fence or index + 1 >= len(lines):
            continue
        nxt = lines[index + 1]
        if not line.strip() or not nxt.strip():
            continue
        if not (_is_md_structural_line(line) and _is_md_structural_line(nxt)):
            out.append("")
    return "\n".join(out)


def _split_weixin_text(text: str, max_chars: int = _WEIXIN_MAX_MSG_CHARS) -> list[str]:
    """把一段完整文本切成多条不超 max_chars 的消息。

    微信流式分段的主边界是"事件类型切换"——一段 LLM 文本/一段工具摘要/
    一段通知天然各自成消息,不会从中间切开。仅当该完整段本身就超过
    max_chars 时才兜底切分,切点优先取段落空行,其次换行,再其次句末标点,
    最后硬切。逻辑对齐 Telegram._split_long_text。
    """
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind("\n\n")
        if cut < max_chars // 2:
            cut = window.rfind("\n")
        if cut < max_chars // 2:
            sentence_ends = [
                window.rfind(p)
                for p in ("。", "！", "？", "；", ". ", "! ", "? ", "; ")
            ]
            cut = max(sentence_ends)
            if cut >= max_chars // 2:
                cut += 1  # 包含标点本身
        if cut < max_chars // 2:
            cut = max_chars
        parts.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


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
        # 每个 scope 当前生效的角色(渠道内 /role 切换的结果)。
        # 不跟踪的话普通消息硬编码 default 角色,微信里 /role 切换完全无效。
        self._role_overrides: dict[str, str] = {}
        # 对齐 hermes WeixinAdapter:全局出站锁 + 限频熔断,避免 10 条后被 iLink -2 限流打断
        self._send_text_gate = asyncio.Lock()
        self._rate_limit_circuit_until = 0.0
        self._rate_limit_events: list[float] = []

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
        if not hasattr(self, "_role_overrides"):
            # 渠道内 /role 切换的覆盖表(测试可能绕过 __init__ 构造实例,惰性兜底)
            self._role_overrides = {}
        if not hasattr(self, "_send_text_gate"):
            self._send_text_gate = asyncio.Lock()
        if not hasattr(self, "_rate_limit_circuit_until"):
            self._rate_limit_circuit_until = 0.0
        if not hasattr(self, "_rate_limit_events"):
            self._rate_limit_events: list[float] = []

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

    def _rate_limit_cooldown_remaining(self) -> float:
        return max(0.0, self._rate_limit_circuit_until - time.monotonic())

    def _reset_rate_limit_circuit(self) -> None:
        self._rate_limit_events.clear()
        self._rate_limit_circuit_until = 0.0

    def _record_rate_limit_event(self) -> bool:
        now = time.monotonic()
        window_start = now - _WEIXIN_RATE_LIMIT_CIRCUIT_WINDOW
        self._rate_limit_events = [ts for ts in self._rate_limit_events if ts >= window_start]
        self._rate_limit_events.append(now)
        if len(self._rate_limit_events) >= _WEIXIN_RATE_LIMIT_CIRCUIT_THRESHOLD:
            self._rate_limit_circuit_until = max(
                self._rate_limit_circuit_until, now + _WEIXIN_RATE_LIMIT_CIRCUIT_OPEN
            )
            return True
        return False

    @staticmethod
    def _is_stale_session_ret(ret: int | None, errcode: int | None, errmsg: str | None) -> bool:
        if ret != _WEIXIN_RATE_LIMIT_ERRCODE and errcode != _WEIXIN_RATE_LIMIT_ERRCODE:
            return False
        return (errmsg or "").lower() == "unknown error"

    async def _send_text(self, prepared: _PreparedWeixinMessage, text: str) -> None:
        """微信出站文本统一入口 — 对齐 hermes gateway/platforms/weixin.py 的限流/熔断策略。

        * 全局 `_send_text_gate` 串行化,避免并发 10 条齐发触发 -2
        * 单条 2000 字符 + 1.5s 间隔
        * 4 次重试, -2 限频时 3 倍退避 + 熔断窗口 30s
        * -14 / stale-session (-2 + unknown error) 时清除 token 并重试一次无 token
        """
        display = _ensure_weixin_line_breaks(text)
        async with self._send_text_gate:
            await self._send_text_locked(prepared, display)

    async def _send_text_locked(self, prepared: _PreparedWeixinMessage, display: str) -> None:
        # 频率控制:相邻发送间隔 _WEIXIN_SEND_MIN_INTERVAL (hermes 1.5s)
        last = getattr(self, "_last_weixin_send_at", 0.0)
        wait = _WEIXIN_SEND_MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        if self._rate_limit_cooldown_remaining() > 0:
            raise RuntimeError(
                f"iLink sendmessage rate limited; cooldown active for {self._rate_limit_cooldown_remaining():.1f}s"
            )

        async def _checked_send(ctx_token: str | None) -> dict:
            resp = await self.client.send_message(
                to_user_id=prepared.to_user,
                text=display,
                context_token=ctx_token,
                client_id=f"openhachimi-{uuid.uuid4().hex[:8]}",
            )
            return resp if isinstance(resp, dict) else {}

        last_exc: Exception | None = None
        context_token: str | None = prepared.context_token
        retried_without_token = False
        for attempt in range(_WEIXIN_SEND_MAX_RETRIES + 1):
            if self._rate_limit_cooldown_remaining() > 0:
                raise RuntimeError(
                    f"iLink sendmessage rate limited; cooldown active for {self._rate_limit_cooldown_remaining():.1f}s"
                )
            try:
                resp = await _checked_send(context_token)
                ret = resp.get("ret")
                errcode = resp.get("errcode")
                errmsg = resp.get("errmsg") or resp.get("msg")
                if ret not in (0, None) or errcode not in (0, None):
                    is_session_expired = (
                        ret == _WEIXIN_SESSION_EXPIRED_ERRCODE
                        or errcode == _WEIXIN_SESSION_EXPIRED_ERRCODE
                        or self._is_stale_session_ret(ret, errcode, errmsg)
                    )
                    if is_session_expired and not retried_without_token and context_token:
                        retried_without_token = True
                        context_token = None
                        logger.warning("[weixin] session expired for %s; retrying without context_token", prepared.to_user[:8])
                        continue
                    is_rate_limited = ret == _WEIXIN_RATE_LIMIT_ERRCODE or errcode == _WEIXIN_RATE_LIMIT_ERRCODE
                    if is_rate_limited:
                        errmsg_text = errmsg or "rate limited"
                        last_exc = RuntimeError(f"iLink sendmessage rate limited: ret={ret} errcode={errcode} errmsg={errmsg_text}")
                        if self._record_rate_limit_event():
                            last_exc = RuntimeError(
                                f"iLink sendmessage rate limited; cooldown active for {self._rate_limit_cooldown_remaining():.1f}s"
                            )
                            break
                        if attempt >= _WEIXIN_SEND_MAX_RETRIES:
                            break
                        wait = 1.0 * 3  # hermes 3x backoff
                        logger.warning("[weixin] rate limited for %s; backing off %.1fs before retry", prepared.to_user[:8], wait)
                        await asyncio.sleep(wait)
                        continue
                    raise RuntimeError(f"iLink sendmessage error: ret={ret} errcode={errcode} errmsg={errmsg}")
                self._reset_rate_limit_circuit()
                self._last_weixin_send_at = time.monotonic()  # type: ignore[attr-defined]
                return
            except Exception as exc:
                # 网络/HTTP 异常
                last_exc = exc
                msg = str(exc)
                # 限流特征已在上层处理,这里仅对网络错误重试
                if "rate limited" in msg.lower() or "cooldown active" in msg.lower():
                    break
                if attempt >= _WEIXIN_SEND_MAX_RETRIES:
                    break
                wait = 1.0 * (attempt + 1)
                logger.warning("[weixin] send chunk failed to=%s attempt=%d/%d, retrying in %.2fs: %s", prepared.to_user[:8], attempt + 1, _WEIXIN_SEND_MAX_RETRIES + 1, wait, msg[:200])
                if wait > 0:
                    await asyncio.sleep(wait)
        assert last_exc is not None
        raise last_exc

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
        self._ensure_media_context_state()

        channel_context = {
            "type": "weixin",
            "platform": "weixin",
            "channel_code": "weixin",
            "session_scope_key": prepared.scope_key,
            # to_user/context_token 供定时任务投递回微信使用(存入 task.origin)。
            "to_user": prepared.to_user,
            "context_token": prepared.context_token,
        }
        current_role = self._role_overrides.get(prepared.scope_key, self.config.default_role_name)

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
                if outcome.role:
                    # /role 切换:记录到 scope 覆盖表,后续普通消息沿用新角色。
                    self._role_overrides[prepared.scope_key] = outcome.role
                reply = outcome.message or "已完成。"
                await self._send_text(prepared, reply)
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
            await self._stream_reply(prepared, text_content, current_role, channel_context)
        except Exception as exc:
            # 兜底:agent 超时/模型报错此前只写日志,微信用户发消息后永远收不到回音。
            logger.exception("微信消息处理失败 来自 %s", prepared.from_user)
            error_text = self._format_error_reply(exc)
            with suppress(Exception):
                await self._send_text(prepared, error_text)
            return
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

    async def _stream_reply(
        self,
        prepared: _PreparedWeixinMessage,
        text_content: str,
        current_role: str,
        channel_context: dict[str, Any],
    ) -> None:
        """对齐 hermes B 方案:微信单轮聚合发送,不超过 10 条。

        * Hermes gateway/platforms/weixin.py 默认 compact 打包,全量文本
          仅在结束时按 2000 字符块打包发送;工具进度完全不发(其他渠道
          Telegram/WebUI 仍展示)。
        * 本实现:流式期间仅累积 text/notice/clarification,tool 仅重置
          presenter;结束时将全文按 _split_weixin_text 打包为 ≤10 条,
          artifact 另计,超限时截断并在末条附提示。
        * 单条发送走 _send_text 的 1.5s 节流 + 4 次重试 + -2 熔断 + -14
          无 token 回退,避免 10 条内再次限流。
        """
        presenter = ToolProgressPresenter(mode="conversation")
        text_parts: list[str] = []
        notice_parts: list[str] = []
        clarification_text: str | None = None
        clarification_choices: list[str] | None = None
        artifacts_collected: list[ArtifactRef] = []
        sent_artifact_paths: set[str] = set()

        async for event in self.service.stream_events(
            text_content,
            current_role,
            session_id=None,
            attachments=prepared.attachments,
            channel_context=channel_context,
            channel="weixin",
        ):
            if not isinstance(event, StreamEventItem):
                continue
            for action in presenter.handle_event(event):
                if action.type == "tool":
                    # B 方案:微信不发 tool,仅重置(对齐 hermes)
                    presenter.reset_tools()
                    logger.debug("微信跳过 tool 进度 %s", action.text[:120])
                elif action.type == "text":
                    text_parts.append(action.text)
                elif action.type == "notice":
                    notice_parts.append(action.text)
                elif action.type == "system":
                    notice_parts.append(action.text)
                elif action.type == "clarification":
                    clarification_text = action.text.strip()
                    clarification_choices = action.choices
                elif action.type == "artifact" and action.artifact:
                    if action.artifact.local_path not in sent_artifact_paths:
                        sent_artifact_paths.add(action.artifact.local_path)
                        artifacts_collected.append(action.artifact)

        presenter.finalize()  # 清理状态,不发

        # 组装终局文本: text + notice + clarification 按序拼接
        final_blocks: list[str] = []
        text_content_final = "".join(text_parts).strip()
        if text_content_final:
            final_blocks.append(text_content_final)
        notice_content = "".join(notice_parts).strip()
        if notice_content:
            final_blocks.append(notice_content)
        if clarification_text:
            q = clarification_text
            if clarification_choices:
                from openhachimi_agent.tools.clarification import format_choices_hint
                q = f"{q}\n\n{format_choices_hint(list(clarification_choices))}".strip()
            final_blocks.append(q)
        final_text = "\n\n".join(b for b in final_blocks if b).strip()

        sent_any = False
        send_index = 0

        async def _send_with_budget(text: str) -> None:
            nonlocal sent_any, send_index
            if not text.strip():
                return
            parts = _split_weixin_text(text)
            for part in parts:
                # 预算:文本 + artifact 合计 ≤10,预留 artifact 位置
                remaining_budget = 10 - len(artifacts_collected) - send_index
                if remaining_budget <= 0:
                    logger.warning("微信已达 10 条上限,截断剩余文本 len=%d", len(part))
                    break
                if send_index >= 9 and len(parts) > remaining_budget:
                    # 最后一条附截断提示,避免静默丢弃
                    part = part[: _WEIXIN_MAX_MSG_CHARS - 60] + "\n\n…（内容过长，已截断，请回复“继续”查看后续）"
                send_index += 1
                preview = part[:80].replace("\n", " ")
                logger.info("微信聚合发送 #%d/10 给 %s 长度=%d 预览=%s", send_index, prepared.to_user, len(part), preview)
                try:
                    await self._send_text(prepared, part)
                    sent_any = True
                except Exception as e:
                    logger.exception("微信聚合发送 #%d 失败: %s", send_index, e)
                    sent_any = True

        # 文本打包发送(预算内)
        await _send_with_budget(final_text)

        # 附件发送:每 artifact 一条,计入预算
        for artifact in artifacts_collected:
            if send_index >= 10:
                logger.warning("微信 10 条已满,跳过 artifact %s", artifact.filename)
                # 降级为文本提示
                notice = _format_artifact_notice([artifact])
                if notice and send_index < 10:
                    await _send_with_budget(notice)
                break
            try:
                await self._send_artifacts(prepared, [artifact])
                send_index += 1
                sent_any = True
                logger.info("微信已发送 artifact %s #%d/10", artifact.filename, send_index)
            except Exception as e:
                logger.exception("微信 artifact 发送失败 %s: %s", artifact.filename, e)
                sent_any = True

        if not sent_any:
            try:
                await self._send_text(prepared, "已完成。")
                sent_any = True
            except Exception as e:
                logger.exception("微信兜底发送失败: %s", e)
        logger.info("微信聚合完成 给 %s 共发送 %d/10 段 文本块=%d artifact=%d", prepared.to_user, send_index, len(final_blocks), len(artifacts_collected))

    @staticmethod
    def _format_error_reply(exc: BaseException) -> str:
        """把 agent 执行异常转成给微信用户看的简短文案。"""
        import asyncio as _asyncio

        if isinstance(exc, _asyncio.TimeoutError) or "超时" in str(exc):
            return (
                "⚠️ 本次处理超时了。任务可能过于复杂，请尝试：\n"
                "1）把任务拆成更小的步骤分别发送；\n"
                "2）稍后重试。"
            )
        text = str(exc) or exc.__class__.__name__
        if len(text) > 200:
            text = text[:200] + "…"
        return f"⚠️ 处理消息时出错：{text}\n请稍后重试，或换一种描述方式。"

    async def send_delivery_text(self, to_user: str, context_token: str, text: str) -> None:
        """供定时任务投递(WeixinDeliverySender)发送文本到指定微信目标。"""
        await self.client.send_message(
            to_user_id=to_user,
            text=_ensure_weixin_line_breaks(text),
            context_token=context_token,
            client_id=f"openhachimi-{uuid.uuid4().hex[:8]}",
        )

    async def _send_artifacts(self, prepared: _PreparedWeixinMessage, artifacts: list[ArtifactRef]) -> None:
        if not artifacts:
            return
        failed: list[ArtifactRef] = []
        for artifact in artifacts:
            try:
                path = weixin_media.resolve_artifact_path(self.config.base_dir, artifact.local_path)
                if not path.is_file():
                    raise FileNotFoundError(f"文件不存在：{path}")
                await weixin_media.send_artifact(
                    self.client,
                    to_user_id=prepared.to_user,
                    context_token=prepared.context_token,
                    path=path,
                    filename=artifact.filename,
                )
                logger.info("已通过微信发送生成文件 %s 给 %s", artifact.filename, prepared.to_user)
            except Exception as exc:
                logger.warning("微信发送生成文件失败 filename=%s: %s", artifact.filename, exc)
                failed.append(artifact)
        notice = _format_artifact_notice(failed)
        if notice:
            await self._send_text(prepared, notice)

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


class _WeixinDeliveryBroker:
    """把投递请求路由到当前在线的 WeixinChannel 实例。

    supervisor 启停渠道时更新 self.channel;lifespan 把 broker 交给
    WeixinDeliverySender,渠道重启无需重新注册。
    """

    def __init__(self) -> None:
        self.channel: WeixinChannel | None = None

    async def __call__(self, to_user: str, context_token: str, text: str) -> None:
        channel = self.channel
        if channel is None or not channel.client.token:
            raise RuntimeError("微信渠道未在线（未登录或已掉线）")
        await channel.send_delivery_text(to_user, context_token, text)


async def _weixin_channel_supervisor(
    service: AgentService,
    config: AppConfig,
    poll_interval: float = _ACCOUNT_WATCH_INTERVAL_SECONDS,
    broker: _WeixinDeliveryBroker | None = None,
) -> None:
    account_path = _account_file(config)
    channel: WeixinChannel | None = None
    channel_task: asyncio.Task | None = None
    active_signature: tuple[int, int] | None = None
    failed_signature: tuple[int, int] | None = None
    restart_attempts = 0
    missing_logged = False

    def _clear_broker() -> None:
        if broker is not None:
            broker.channel = None

    try:
        while True:
            signature = _account_signature(account_path)

            if channel_task is not None and channel_task.done():
                ran_seconds = 0.0
                if channel is not None and getattr(channel, "started_at", None):
                    ran_seconds = time.monotonic() - channel.started_at
                await _stop_channel_task(channel_task, channel)
                _clear_broker()
                # 渠道任务退出:区分"刚启动就死"(连续快速失败,退避重试)与
                # "运行一段时间后偶发退出"(立即重启)。同一签名连续失败 3 次
                # 后进入长退避,避免死循环刷日志。
                if signature is not None and signature == active_signature:
                    if ran_seconds > 60:
                        restart_attempts = 0
                    restart_attempts += 1
                    if restart_attempts > 3:
                        failed_signature = signature
                        logger.error(
                            "微信渠道连续失败 %d 次,暂停自动重启;重新登录(hachimi weixin)后恢复。", restart_attempts
                        )
                    else:
                        backoff = min(60.0, restart_attempts * 10.0)
                        logger.warning(
                            "微信渠道异常退出(运行 %.0fs),%.0fs 后自动重启(第 %d 次)。",
                            ran_seconds, backoff, restart_attempts,
                        )
                        await asyncio.sleep(backoff)
                channel = None
                channel_task = None
                active_signature = None

            if signature is None:
                if channel_task is not None:
                    logger.info("微信账号文件已移除，正在停止微信渠道：%s", account_path)
                    await _stop_channel_task(channel_task, channel)
                    _clear_broker()
                    channel = None
                    channel_task = None
                    active_signature = None
                    failed_signature = None
                    restart_attempts = 0
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
                    _clear_broker()
                    channel = None
                    channel_task = None
                    active_signature = None
                    failed_signature = None
                    restart_attempts = 0

                if channel_task is None and signature != failed_signature:
                    logger.info("检测到微信账号文件 (%s)，正在启动微信渠道...", account_path)
                    channel = WeixinChannel(service, config)
                    channel.started_at = time.monotonic()
                    channel_task = asyncio.create_task(channel.run_loop())
                    active_signature = signature
                    if broker is not None:
                        broker.channel = channel

            await asyncio.sleep(poll_interval)
    finally:
        await _stop_channel_task(channel_task, channel)
        _clear_broker()


@asynccontextmanager
async def weixin_lifespan(app):
    config: AppConfig = app.state.config
    service: AgentService = app.state.service

    # broker 暴露给投递系统,使微信渠道创建的定时任务可以把结果发回微信。
    broker = _WeixinDeliveryBroker()
    app.state.weixin_delivery_broker = broker
    supervisor_task = asyncio.create_task(_weixin_channel_supervisor(service, config, broker=broker))

    yield broker

    supervisor_task.cancel()
    with suppress(asyncio.CancelledError):
        await supervisor_task
