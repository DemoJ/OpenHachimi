"""Git 查询工具。"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.tools.utils import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    normalize_relative_path,
    relative_path_from,
    resolve_workspace_path,
    run_subprocess,
)


logger = logging.getLogger(__name__)


def git_status(ctx: RunContext[AppConfig], cwd: str = ".") -> dict[str, object]:
    """查看当前工作区的 Git 状态。"""
    logger.debug("tool git_status cwd=%s", cwd)
    target_cwd = resolve_workspace_path(ctx.deps.base_dir, cwd)
    if not target_cwd.is_dir():
        raise NotADirectoryError(f"工作目录不是目录：{cwd}")

    result = run_subprocess(
        ["git", "status", "--short", "--branch"],
        cwd=target_cwd,
        timeout_seconds=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    result["cwd"] = normalize_relative_path(ctx.deps.base_dir, target_cwd) if target_cwd != ctx.deps.base_dir else "."
    status_lines = [line for line in str(result["stdout"]).splitlines() if line.strip()]
    result["clean"] = bool(status_lines) and all(line.startswith("## ") for line in status_lines)
    return result


def git_diff(
    ctx: RunContext[AppConfig],
    path: str | None = None,
    staged: bool = False,
    ref: str | None = None,
    cwd: str = ".",
) -> dict[str, object]:
    """查看 Git diff，可查看未暂存、已暂存或相对某个引用的差异。"""
    logger.debug("tool git_diff path=%s staged=%s ref=%s cwd=%s", path, staged, ref, cwd)
    target_cwd = resolve_workspace_path(ctx.deps.base_dir, cwd)
    if not target_cwd.is_dir():
        raise NotADirectoryError(f"工作目录不是目录：{cwd}")

    command = ["git", "diff"]
    if ref:
        command.append(ref)
    elif staged:
        command.append("--cached")

    if path:
        target_path = resolve_workspace_path(ctx.deps.base_dir, path)
        command.extend(["--", relative_path_from(target_cwd, target_path)])

    result = run_subprocess(
        command,
        cwd=target_cwd,
        timeout_seconds=DEFAULT_COMMAND_TIMEOUT_SECONDS,
    )
    result["cwd"] = normalize_relative_path(ctx.deps.base_dir, target_cwd) if target_cwd != ctx.deps.base_dir else "."
    result["staged"] = staged
    result["ref"] = ref
    result["path"] = path
    return result
