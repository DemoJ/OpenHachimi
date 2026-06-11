"""Agent 生成文件的发布工具。"""

from __future__ import annotations

import mimetypes
import re
import shutil
import uuid
from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.utils import normalize_relative_path, resolve_workspace_path
from openhachimi_agent.transport.api_models import ArtifactRef

SENSITIVE_DIR_NAMES = {".git", ".memory", ".venv", "__pycache__", ".browser_data"}
SENSITIVE_NAME_RE = re.compile(r"(?i)(^\.env$|credential|secret|token|private[_-]?key|password|passwd)")
SAFE_FILENAME_RE = re.compile(r"[^\w. -]+", re.UNICODE)


def _sanitize_filename(filename: str) -> str:
    cleaned = SAFE_FILENAME_RE.sub("_", Path(filename).name).strip()
    # 再去掉首尾的点号和下划线（避免产生隐藏文件或无扩展名文件）
    cleaned = cleaned.strip("._")
    return cleaned[:120] or "artifact"


def _reject_sensitive_path(path: Path) -> None:
    if any(part in SENSITIVE_DIR_NAMES for part in path.parts):
        raise ModelRetry("该路径位于敏感目录中，不能发布为文件附件。")
    if SENSITIVE_NAME_RE.search(path.name):
        raise ModelRetry("该文件名疑似包含密钥或凭据，不能发布为文件附件。")


def _artifact_cache_dir(ctx: RunContext[AgentDeps], artifact_id: str) -> Path:
    attachments_dir = getattr(ctx.deps.config, "attachments_dir", ctx.deps.base_dir / ".tmp" / "attachments")
    return attachments_dir.parent / "artifacts" / artifact_id


def _build_artifact_ref(
    ctx: RunContext[AgentDeps],
    target_file: Path,
    *,
    filename: str | None,
    title: str | None,
    description: str | None,
) -> ArtifactRef:
    size_bytes = target_file.stat().st_size
    max_size = ctx.deps.config.max_attachment_size_bytes
    if size_bytes > max_size:
        raise ModelRetry(f"文件过大：{size_bytes} bytes，当前上限为 {max_size} bytes")

    output_filename = _sanitize_filename(filename or target_file.name)
    content_type = mimetypes.guess_type(output_filename)[0] or mimetypes.guess_type(target_file.name)[0] or "application/octet-stream"
    artifact_id = f"art_{uuid.uuid4().hex[:12]}"
    artifact_dir = _artifact_cache_dir(ctx, artifact_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_file = artifact_dir / output_filename
    shutil.copy2(target_file, artifact_file)
    return ArtifactRef(
        id=artifact_id,
        filename=output_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        local_path=normalize_relative_path(ctx.deps.base_dir, artifact_file),
        download_url=f"/artifacts/{artifact_id}/download",
        title=title,
        description=description,
    )


def publish_artifact(
    ctx: RunContext[AgentDeps],
    path: str,
    filename: str | None = None,
    title: str | None = None,
    description: str | None = None,
    cwd: str = ".",
) -> dict[str, object]:
    """将工作区内已有文件发布为可发送给用户的文件附件。"""
    target_cwd = resolve_workspace_path(ctx.deps.base_dir, cwd)
    target_file = resolve_workspace_path(target_cwd, path, [ctx.deps.base_dir, *ctx.deps.skills_dirs])
    if not target_file.exists():
        raise ModelRetry(f"文件不存在：{path}")
    if not target_file.is_file():
        raise ModelRetry(f"目标不是文件：{path}")
    _reject_sensitive_path(target_file.relative_to(ctx.deps.base_dir.resolve()))

    artifact = _build_artifact_ref(ctx, target_file, filename=filename, title=title, description=description)
    turn_artifacts = ctx.deps.session_state.setdefault("turn_artifacts", [])
    artifacts = ctx.deps.session_state.setdefault("artifacts", [])
    if isinstance(turn_artifacts, list):
        turn_artifacts.append(artifact)
    if isinstance(artifacts, list):
        artifacts.append(artifact)
    known_paths = ctx.deps.session_state.setdefault("known_paths", {})
    if isinstance(known_paths, dict):
        known_paths[normalize_relative_path(ctx.deps.base_dir, target_file)] = {
            "action": "publish_artifact",
            "artifact_id": artifact.id,
            "filename": artifact.filename,
        }
    return {"artifact": artifact.model_dump(mode="json")}
