"""主 agent 的附件与视觉辅助:附件元数据拼进 user-prompt、视觉识别状态写入 session_state、
直传图片失败时的降级。从 ``main_agent.py`` 原样拆出,行为不变。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic_ai.messages import UserContent

from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.tools.vision_guard import normalize_vision_guard_path
from openhachimi_agent.transport.api_models import AttachmentRef
from openhachimi_agent.vision.preprocess import VisionPreprocessResult, image_attachments



def _normalize_attachment_path(config: AppConfig, attachment: AttachmentRef) -> str:
    path = Path(attachment.local_path)
    target = path if path.is_absolute() else config.base_dir / path
    return normalize_vision_guard_path(target)


def _vision_state_entry(config: AppConfig, attachment: AttachmentRef, *, status: str, mode: str | None = None) -> dict[str, Any]:
    resolved_path = _normalize_attachment_path(config, attachment)
    return {
        "attachment_id": attachment.id,
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "kind": attachment.kind,
        "local_path": attachment.local_path,
        "resolved_path": resolved_path,
        "mode": mode,
        "status": status,
        "model": None,
        "summary": None,
        "errors": [],
        "size_bytes": attachment.size_bytes,
    }


def _mark_vision_processing(session_state: dict[str, Any], config: AppConfig, attachments: list[AttachmentRef]) -> None:
    images = image_attachments(attachments)
    if not images:
        return
    vision_attachments = session_state.setdefault("vision_attachments", {})
    path_index = session_state.setdefault("vision_attachment_paths", {})
    for attachment in images:
        entry = _vision_state_entry(config, attachment, status="processing")
        vision_attachments[attachment.id] = entry
        path_index[entry["resolved_path"]] = attachment.id


def _mark_vision_result(session_state: dict[str, Any], config: AppConfig, attachments: list[AttachmentRef], vision_result: VisionPreprocessResult) -> None:
    images_by_id = {attachment.id: attachment for attachment in image_attachments(attachments)}
    vision_attachments = session_state.setdefault("vision_attachments", {})
    path_index = session_state.setdefault("vision_attachment_paths", {})

    for status in vision_result.attachment_statuses:
        attachment = images_by_id.get(status.attachment_id)
        if attachment is None:
            continue
        entry = dict(vision_attachments.get(attachment.id) or _vision_state_entry(config, attachment, status=status.status, mode=status.mode))
        entry.update(asdict(status))
        entry["kind"] = attachment.kind
        entry["size_bytes"] = attachment.size_bytes
        entry["resolved_path"] = _normalize_attachment_path(config, attachment)
        vision_attachments[attachment.id] = entry
        path_index[entry["resolved_path"]] = attachment.id

    status_ids = {status.attachment_id for status in vision_result.attachment_statuses}
    fallback_status = "unavailable" if vision_result.mode == "unavailable" else vision_result.mode
    for attachment in images_by_id.values():
        if attachment.id in status_ids:
            continue
        entry = dict(vision_attachments.get(attachment.id) or _vision_state_entry(config, attachment, status=fallback_status, mode=vision_result.mode))
        entry.update({"mode": vision_result.mode, "status": fallback_status, "errors": list(vision_result.errors)})
        vision_attachments[attachment.id] = entry
        path_index[entry["resolved_path"]] = attachment.id


def _vision_result_summary(vision_result: VisionPreprocessResult) -> tuple[int, int]:
    succeeded = sum(1 for status in vision_result.attachment_statuses if status.mode == "fallback" and status.status == "succeeded")
    failed = sum(1 for status in vision_result.attachment_statuses if status.status in {"failed", "unavailable"})
    return succeeded, failed


def format_attachments_for_prompt(attachments: list[AttachmentRef], vision_result: VisionPreprocessResult | None = None) -> str:
    if not attachments:
        return ""

    consumed_ids = set(vision_result.consumed_attachment_ids) if vision_result else set()
    vision_mode = vision_result.mode if vision_result else "none"
    if consumed_ids and vision_mode == "fallback":
        handling_hint = (
            "图片附件已由辅助视觉模型识别，识别结果已在上文提供。"
            "不要再调用 inspect_image、read_file、browser_navigate 或其他工具读取这些图片；"
            "请直接基于辅助视觉模型输出和用户问题继续处理。"
        )
    elif consumed_ids and vision_mode == "direct":
        handling_hint = "图片附件已随本轮消息直接发送给主模型；不要再调用工具读取这些图片。"
    elif consumed_ids and vision_mode == "unavailable":
        handling_hint = (
            "系统已尝试处理图片但未能取得可用视觉内容。"
            "inspect_image、read_file、browser_navigate 不能替代视觉识别；不要调用这些工具假装读取图片内容。"
        )
    else:
        handling_hint = "请不要臆测附件内容；需要内容时调用文件或图片工具。不要在回复中泄露本地绝对路径。"

    lines = [
        "用户同时发送了以下附件：",
        handling_hint,
    ]
    for attachment in attachments:
        status = "已处理" if attachment.id in consumed_ids else "未处理"
        lines.extend(
            [
                f"- id: {attachment.id}",
                f"  status: {status}",
                f"  kind: {attachment.kind}",
                f"  filename: {attachment.filename or 'unknown'}",
                f"  content_type: {attachment.content_type or 'unknown'}",
                f"  size_bytes: {attachment.size_bytes if attachment.size_bytes is not None else 'unknown'}",
                f"  local_path: {attachment.local_path}",
            ]
        )
    return "\n".join(lines)


def message_with_attachments(message: str, attachments: list[AttachmentRef], vision_result: VisionPreprocessResult | None = None) -> str:
    """构造给模型的 user-prompt 文本:用户原话 + 附件元数据块(+ 视觉前缀)。"""
    attachment_block = format_attachments_for_prompt(attachments, vision_result)
    user_message = message.strip() or "用户发送了附件，请根据附件内容协助处理。"
    prefix = vision_result.text_prefix.strip() if vision_result and vision_result.text_prefix.strip() else ""
    parts = [part for part in [prefix, user_message, attachment_block] if part]
    if not parts:
        return message
    return "\n\n".join(parts)


def _with_direct_vision_parts(text: str, vision_result: VisionPreprocessResult | None) -> str | list[UserContent]:
    if not vision_result or not vision_result.direct_parts:
        return text
    return [text, *vision_result.direct_parts]


def _degrade_direct_vision_result(vision_result: VisionPreprocessResult, error: Exception) -> VisionPreprocessResult:
    """主模型直传图片失败后，降级为纯文本上下文，避免整轮对话崩溃。"""
    error_text = str(error).strip() or error.__class__.__name__
    text_prefix = render_system_prompt("vision/direct_error_prefix", {"error_text": error_text}) + "\n"
    return VisionPreprocessResult(
        mode="unavailable",
        text_prefix=text_prefix,
        consumed_attachment_ids=vision_result.consumed_attachment_ids,
        errors=[*vision_result.errors, error_text],
    )
