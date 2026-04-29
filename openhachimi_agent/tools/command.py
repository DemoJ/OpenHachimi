"""工作区命令执行工具。"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.tools.utils import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    assert_safe_command,
    get_command_shell,
    normalize_relative_path,
    resolve_workspace_path,
    run_subprocess,
)


logger = logging.getLogger(__name__)


def run_command(
    ctx: RunContext[AppConfig],
    command: str,
    cwd: str = ".",
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """在工作区内执行非交互式系统命令。

    Windows 下默认使用 PowerShell 或 pwsh。
    Linux/macOS 下默认使用当前 SHELL，找不到时回退到 /bin/sh。
    """
    if not command.strip():
        raise ValueError("command 不能为空")

    logger.info("tool run_command cwd=%s timeout_seconds=%d command=%s", cwd, timeout_seconds, command)
    assert_safe_command(command)
    target_cwd = resolve_workspace_path(ctx.deps.base_dir, cwd)
    if not target_cwd.exists():
        raise FileNotFoundError(f"工作目录不存在：{cwd}")
    if not target_cwd.is_dir():
        raise NotADirectoryError(f"工作目录不是目录：{cwd}")

    timeout_seconds = max(1, min(timeout_seconds, DEFAULT_COMMAND_TIMEOUT_SECONDS))
    shell_command, shell_name = get_command_shell()
    result = run_subprocess(
        [*shell_command, command],
        cwd=target_cwd,
        timeout_seconds=timeout_seconds,
    )
    result["cwd"] = normalize_relative_path(ctx.deps.base_dir, target_cwd) if target_cwd != ctx.deps.base_dir else "."
    result["shell"] = shell_name
    result["command_text"] = command
    logger.info(
        "tool run_command finished cwd=%s shell=%s exit_code=%s timed_out=%s",
        result["cwd"],
        shell_name,
        result["exit_code"],
        result["timed_out"],
    )
    return result
