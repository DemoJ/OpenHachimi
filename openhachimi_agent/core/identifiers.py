"""Validation helpers for externally supplied identifiers and paths."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PureWindowsPath

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _has_path_semantics(value: str) -> bool:
    raw_path = Path(value)
    win_path = PureWindowsPath(value)
    return raw_path.is_absolute() or win_path.is_absolute() or len(raw_path.parts) > 1 or len(win_path.parts) > 1


def _reject_common_identifier_issues(value: str, label: str, *, max_length: int = 128) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        raise ValueError(f"{label} 不能为空。")
    if len(candidate) > max_length:
        raise ValueError(f"{label} 过长。")
    if _CONTROL_RE.search(candidate):
        raise ValueError(f"{label} 包含非法控制字符。")
    if candidate in {".", ".."} or ".." in Path(candidate).parts or ".." in PureWindowsPath(candidate).parts:
        raise ValueError(f"{label} 不能包含路径跳转片段。")
    if _has_path_semantics(candidate):
        raise ValueError(f"{label} 不能包含路径或路径分隔符。")
    stem = PureWindowsPath(candidate).stem.upper()
    if stem in WINDOWS_DEVICE_NAMES:
        raise ValueError(f"{label} 不能使用 Windows 保留设备名。")
    if candidate.endswith((" ", ".")):
        raise ValueError(f"{label} 不能以空格或点结尾。")
    return candidate


def validate_role_name(role_name: str) -> str:
    return _reject_common_identifier_issues(role_name, "角色名称", max_length=128)


def validate_session_id(session_id: str, *, allow_legacy: bool = False) -> str:
    candidate = _reject_common_identifier_issues(session_id, "会话 ID", max_length=128)
    if candidate == "legacy" and allow_legacy:
        return candidate
    if candidate == "legacy" and not allow_legacy:
        raise ValueError("会话 ID 格式不合法。")
    if not _SESSION_RE.fullmatch(candidate):
        raise ValueError("会话 ID 只能包含字母、数字、点、下划线、短横线和冒号。")
    return candidate


def validate_latest_scope(scope: str | None) -> str | None:
    if scope is None:
        return None
    candidate = _reject_common_identifier_issues(scope, "会话范围", max_length=128)
    if not _SCOPE_RE.fullmatch(candidate):
        raise ValueError("会话范围只能包含字母、数字、点、下划线、短横线和冒号。")
    return candidate


def scope_digest(scope: str) -> str:
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]


def ensure_path_under(root: Path, target: Path, *, label: str = "路径") -> Path:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} 超出允许目录。") from exc
    return resolved_target


def safe_role_file_path(roles_dir: Path, role_name: str) -> Path:
    role = validate_role_name(role_name)
    return ensure_path_under(roles_dir, roles_dir / f"{role}.md", label="角色配置路径")
