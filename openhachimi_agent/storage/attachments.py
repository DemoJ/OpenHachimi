"""附件存储与安全校验。"""

from __future__ import annotations

import re
import uuid
from pathlib import Path, PureWindowsPath

from openhachimi_agent.transport.api_models import AttachmentRef

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
}
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class AttachmentError(ValueError):
    pass


class AttachmentStorage:
    def __init__(self, base_dir: Path, max_size_bytes: int, allowed_mime_types: list[str] | None = None) -> None:
        self.base_dir = base_dir
        self.max_size_bytes = max_size_bytes
        self.allowed_mime_types = {mime.lower() for mime in allowed_mime_types or []}

    def validate_metadata(self, *, filename: str | None, content_type: str | None, size_bytes: int | None) -> None:
        if size_bytes is not None and size_bytes > self.max_size_bytes:
            raise AttachmentError(f"附件过大：{size_bytes} bytes，当前上限为 {self.max_size_bytes} bytes")
        if filename:
            raw_path = Path(filename)
            win_path = PureWindowsPath(filename)
            if raw_path.is_absolute() or win_path.is_absolute() or len(raw_path.parts) > 1 or len(win_path.parts) > 1:
                raise AttachmentError("附件文件名不能包含路径")

    def sanitize_filename(self, filename: str | None, content_type: str | None) -> str:
        candidate = (filename or "attachment").strip().replace("\x00", "")
        candidate = Path(PureWindowsPath(candidate).name).name
        candidate = SAFE_FILENAME_RE.sub("_", candidate).strip(" ._")
        if not candidate:
            candidate = "attachment"

        stem = Path(candidate).stem[:80].strip(" ._") or "attachment"
        suffix = Path(candidate).suffix.lower()
        if not suffix and content_type:
            suffix = MIME_EXTENSIONS.get(content_type.lower(), "")
        if len(suffix) > 16:
            suffix = ""
        if stem.upper() in WINDOWS_DEVICE_NAMES:
            stem = f"{stem}_file"
        return f"{stem}{suffix}"

    def build_path(self, *, source: str, namespace: str, filename: str | None, content_type: str | None) -> Path:
        safe_source = SAFE_FILENAME_RE.sub("_", source).strip(" ._") or "source"
        safe_namespace = SAFE_FILENAME_RE.sub("_", namespace).strip(" ._") or "session"
        safe_filename = self.sanitize_filename(filename, content_type)
        attachment_dir = self.base_dir / safe_source / safe_namespace
        candidate = attachment_dir / safe_filename
        resolved_base = self.base_dir.resolve()
        resolved_candidate = candidate.resolve()
        try:
            resolved_candidate.relative_to(resolved_base)
        except ValueError as exc:
            raise AttachmentError("附件保存路径超出允许目录") from exc
        if resolved_candidate.exists():
            resolved_candidate = resolved_candidate.with_name(f"{resolved_candidate.stem}_{uuid.uuid4().hex[:8]}{resolved_candidate.suffix}")
        return resolved_candidate

    def to_ref(
        self,
        *,
        path: Path,
        source: str,
        filename: str | None,
        content_type: str | None,
        size_bytes: int | None,
        metadata: dict[str, object] | None = None,
    ) -> AttachmentRef:
        actual_size = path.stat().st_size if path.exists() else size_bytes
        if actual_size is not None and actual_size > self.max_size_bytes:
            raise AttachmentError(f"附件过大：{actual_size} bytes，当前上限为 {self.max_size_bytes} bytes")
        kind = "image" if content_type and content_type.lower() in IMAGE_MIME_TYPES else "document"
        try:
            local_path = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            local_path = path.as_posix()
        return AttachmentRef(
            id=f"att_{uuid.uuid4().hex[:12]}",
            filename=filename or path.name,
            content_type=content_type,
            size_bytes=actual_size,
            local_path=local_path,
            source=source if source in {"telegram", "http", "local"} else "local",
            kind=kind,
            metadata=dict(metadata or {}),
        )
