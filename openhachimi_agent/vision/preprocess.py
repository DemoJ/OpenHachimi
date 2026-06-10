"""图片附件预处理。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic_ai.messages import BinaryContent, UserContent

from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.tools.utils import resolve_workspace_path
from openhachimi_agent.transport.api_models import AttachmentRef
from openhachimi_agent.vision.capabilities import VisionMode, resolve_vision_mode_auto
from openhachimi_agent.vision.openai_compatible import VisionImagePayload, VisionModelError, request_vision

logger = logging.getLogger(__name__)

MAX_VISION_DESCRIPTION_CHARS = 4000


@dataclass(frozen=True)
class VisionAttachment:
    attachment: AttachmentRef
    path: Path
    content_type: str


VisionAttachmentStatusValue = Literal["processing", "succeeded", "failed", "direct", "unavailable"]


@dataclass(frozen=True)
class VisionAttachmentStatus:
    attachment_id: str
    local_path: str
    filename: str | None
    content_type: str | None
    mode: VisionMode
    status: VisionAttachmentStatusValue
    model: str | None = None
    summary: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VisionPreprocessResult:
    mode: VisionMode = "none"
    text_prefix: str = ""
    direct_parts: list[UserContent] = field(default_factory=list)
    consumed_attachment_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    attachment_statuses: list[VisionAttachmentStatus] = field(default_factory=list)


def image_attachments(attachments: list[AttachmentRef]) -> list[AttachmentRef]:
    return [attachment for attachment in attachments if attachment.kind == "image"]


def _resolve_image_attachment(config: AppConfig, attachment: AttachmentRef) -> VisionAttachment:
    target = resolve_workspace_path(
        config.base_dir,
        attachment.local_path,
        [config.attachments_dir],
    )
    if not target.exists() or not target.is_file():
        raise VisionModelError(f"图片文件不存在：{attachment.filename or attachment.id}")
    size = target.stat().st_size
    if size > config.vision.max_image_size_bytes:
        raise VisionModelError(
            f"图片过大：{attachment.filename or attachment.id} 为 {size} bytes，"
            f"当前视觉上限为 {config.vision.max_image_size_bytes} bytes"
        )
    content_type = attachment.content_type or "image/jpeg"
    if not content_type.startswith("image/"):
        raise VisionModelError(f"附件不是图片 MIME 类型：{attachment.filename or attachment.id}")
    return VisionAttachment(attachment=attachment, path=target, content_type=content_type)


def _collect_images(config: AppConfig, attachments: list[AttachmentRef]) -> tuple[list[VisionAttachment], list[str]]:
    resolved: list[VisionAttachment] = []
    errors: list[str] = []
    for attachment in image_attachments(attachments)[: config.vision.max_images_per_message]:
        try:
            resolved.append(_resolve_image_attachment(config, attachment))
        except VisionModelError as exc:
            errors.append(str(exc))
        except Exception as exc:
            logger.error("unexpected error processing attachment_id=%s: %s", attachment.id, exc, exc_info=True)
            errors.append(f"内部错误：{attachment.filename or attachment.id}: {exc}")
    skipped = max(0, len(image_attachments(attachments)) - config.vision.max_images_per_message)
    if skipped:
        errors.append(f"已跳过 {skipped} 张超出 vision.max_images_per_message 限制的图片")
    return resolved, errors


def _attachment_status(
    item: VisionAttachment,
    *,
    mode: VisionMode,
    status: VisionAttachmentStatusValue,
    model: str | None = None,
    summary: str | None = None,
    errors: list[str] | None = None,
) -> VisionAttachmentStatus:
    return VisionAttachmentStatus(
        attachment_id=item.attachment.id,
        local_path=item.attachment.local_path,
        filename=item.attachment.filename,
        content_type=item.content_type,
        mode=mode,
        status=status,
        model=model,
        summary=summary,
        errors=list(errors or []),
    )


def _unavailable_prefix(errors: list[str]) -> str:
    details = "\n".join(f"- {error}" for error in errors if error)
    if details:
        details = f"\n{details}"
    return render_system_prompt("vision/unavailable_prefix", {"details": details}) + "\n"


def _isolate_vision_description(description: str) -> str:
    """隔离辅助视觉模型输出，避免其被主模型误解为上层指令。"""
    text = description.strip()
    truncated = len(text) > MAX_VISION_DESCRIPTION_CHARS
    if truncated:
        text = text[:MAX_VISION_DESCRIPTION_CHARS].rstrip() + "\n...[视觉模型输出已截断]"
    return f"[视觉模型输出开始]\n{text}\n[视觉模型输出结束]"


def _fallback_prefix(descriptions: list[str], errors: list[str], model: str) -> str:
    errors_text = ""
    if errors:
        errors_text = "\n部分图片处理失败：\n" + "\n".join(f"- {error}" for error in errors)
    return render_system_prompt(
        "vision/fallback_prefix",
        {
            "model": model,
            "descriptions": "\n".join(descriptions),
            "errors": errors_text,
        },
    ).strip() + "\n"


async def preprocess_vision_attachments(
    *,
    config: AppConfig,
    message: str,
    attachments: list[AttachmentRef],
) -> VisionPreprocessResult:
    """按配置处理图片附件，返回可注入主模型的多模态或文本上下文。"""
    images = image_attachments(attachments)
    if not images:
        return VisionPreprocessResult(mode="none")

    resolved_images, errors = _collect_images(config, attachments)
    if not resolved_images:
        return VisionPreprocessResult(mode="unavailable", text_prefix=_unavailable_prefix(errors), errors=errors)

    probe_image = VisionImagePayload(path=resolved_images[0].path, content_type=resolved_images[0].content_type)
    mode = await resolve_vision_mode_auto(config, has_images=True, probe_image=probe_image)

    consumed_ids = [item.attachment.id for item in resolved_images]
    if mode == "direct":
        parts: list[UserContent] = []
        for item in resolved_images:
            parts.append(
                BinaryContent(
                    data=item.path.read_bytes(),
                    media_type=item.content_type,
                    identifier=item.attachment.id,
                    vendor_metadata={"detail": config.vision.detail},
                )
            )
        prefix = ""
        if errors:
            prefix = "[图片附件处理提示]\n" + "\n".join(f"- {error}" for error in errors) + "\n"
        return VisionPreprocessResult(
            mode="direct",
            text_prefix=prefix,
            direct_parts=parts,
            consumed_attachment_ids=consumed_ids,
            errors=errors,
            attachment_statuses=[
                _attachment_status(item, mode="direct", status="direct", model=config.model_name)
                for item in resolved_images
            ],
        )

    if mode == "fallback":
        descriptions: list[str] = []
        statuses: list[VisionAttachmentStatus] = []
        prompt = config.vision.prompt
        if message.strip():
            prompt = f"{prompt}\n\n用户随图提出的问题：{message.strip()}"
        for index, item in enumerate(resolved_images, start=1):
            try:
                description = await request_vision(
                    config.vision,
                    VisionImagePayload(path=item.path, content_type=item.content_type),
                    prompt,
                )
                safe_description = _isolate_vision_description(description)
                descriptions.append(
                    f"图片 {index}（id: {item.attachment.id}，文件名: {item.attachment.filename or 'unknown'}）：\n{safe_description}"
                )
                statuses.append(
                    _attachment_status(
                        item,
                        mode="fallback",
                        status="succeeded",
                        model=config.vision.model,
                        summary=safe_description,
                    )
                )
                logger.info(
                    "vision fallback succeeded attachment_id=%s model=%s summary_chars=%d",
                    item.attachment.id,
                    config.vision.model,
                    len(safe_description),
                )
            except Exception as exc:
                error = f"{item.attachment.filename or item.attachment.id}: {exc}"
                logger.warning("vision fallback failed attachment_id=%s: %s", item.attachment.id, exc, exc_info=True)
                errors.append(error)
                statuses.append(
                    _attachment_status(
                        item,
                        mode="fallback",
                        status="failed",
                        model=config.vision.model,
                        errors=[error],
                    )
                )
        if descriptions:
            return VisionPreprocessResult(
                mode="fallback",
                text_prefix=_fallback_prefix(descriptions, errors, config.vision.model),
                consumed_attachment_ids=[status.attachment_id for status in statuses if status.status == "succeeded"],
                errors=errors,
                attachment_statuses=statuses,
            )
        return VisionPreprocessResult(
            mode="unavailable",
            text_prefix=_unavailable_prefix(errors),
            errors=errors,
            attachment_statuses=statuses,
        )

    return VisionPreprocessResult(
        mode="unavailable",
        text_prefix=_unavailable_prefix(errors),
        errors=errors,
        attachment_statuses=[
            _attachment_status(item, mode="unavailable", status="unavailable", errors=errors)
            for item in resolved_images
        ],
    )
