"""附件相关只读工具。"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.utils import normalize_relative_path, resolve_workspace_path

IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": "PNG",
    b"\xff\xd8\xff": "JPEG",
    b"GIF87a": "GIF",
    b"GIF89a": "GIF",
    b"RIFF": "WEBP",
}


def _resolve_image_path(ctx: RunContext[AgentDeps], path: str) -> Path:
    allowed_roots = list(ctx.deps.skills_dirs)
    attachments_dir = getattr(ctx.deps.config, "attachments_dir", None)
    if attachments_dir is not None:
        allowed_roots.append(attachments_dir)
    return resolve_workspace_path(ctx.deps.base_dir, path, allowed_roots)


def inspect_image(ctx: RunContext[AgentDeps], path: str) -> dict[str, object]:
    """读取图片文件的基础元数据，不返回图片内容。"""
    target = _resolve_image_path(ctx, path)
    if not target.exists() or not target.is_file():
        raise ModelRetry(f"图片文件不存在：{path}")

    max_size = getattr(ctx.deps.config, "max_attachment_size_bytes", 10 * 1024 * 1024)
    size = target.stat().st_size
    if size > max_size:
        raise ModelRetry(f"图片过大：{size} bytes，当前上限为 {max_size} bytes")

    header = target.read_bytes()[:32]
    detected = None
    for signature, image_format in IMAGE_SIGNATURES.items():
        if header.startswith(signature):
            detected = image_format
            break
    if detected == "WEBP" and header[8:12] != b"WEBP":
        detected = None
    if detected is None:
        raise ModelRetry(f"文件不是支持的图片格式：{path}")

    return {
        "path": normalize_relative_path(ctx.deps.base_dir, target),
        "format": detected,
        "size_bytes": size,
    }
