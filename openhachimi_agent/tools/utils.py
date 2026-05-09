"""工作区工具共享辅助函数。"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import logging
from pathlib import Path
from pydantic_ai.exceptions import ModelRetry

MAX_LIST_ENTRIES = 200
MAX_READ_LINES = 200
MAX_SEARCH_RESULTS = 200
MAX_COMMAND_OUTPUT_CHARS = 12000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
SKIP_DIR_NAMES = {
    ".git",
    ".memory",
    ".tmp",
    ".tmp-ensurepip",
    ".venv",
    "__pycache__",
    "openhachimi_agent.egg-info",
}
DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\b",
    r"\bunlink\b",
    r"\bremove-item\b",
    r"\bdel\b",
    r"\berase\b",
    r"\brmdir\b",
    r"\brd\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b",
    r"\bformat\b",
    r"\bshutdown\b",
    r"\brestart-computer\b",
    r"\bstop-process\b",
]

logger = logging.getLogger(__name__)


def resolve_workspace_path(workspace_root: Path, path: str, allowed_roots: list[Path] | None = None) -> Path:
    """将用户提供的路径解析到工作区内，并阻止越界访问。"""
    raw_path = Path(path)
    resolved = raw_path.resolve() if raw_path.is_absolute() else (workspace_root / raw_path).resolve()

    roots_to_check = [workspace_root.resolve()]
    if allowed_roots:
        roots_to_check.extend([r.resolve() for r in allowed_roots])

    is_allowed = False
    for root in roots_to_check:
        try:
            resolved.relative_to(root)
            is_allowed = True
            break
        except ValueError:
            continue

    if not is_allowed:
        raise ModelRetry(f"路径超出当前工作区及允许的外部目录，不允许访问：{path}")

    return resolved


def normalize_relative_path(workspace_root: Path, path: Path) -> str:
    """将绝对路径转换为工作区相对路径。"""
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return path.as_posix()


def relative_path_from(cwd: Path, target: Path) -> str:
    """计算一个路径相对指定目录的相对表示。"""
    return Path(os.path.relpath(target, cwd)).as_posix()


def should_skip_path(path: Path) -> bool:
    """判断路径是否应从搜索结果中跳过。"""
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def iter_workspace_items(root: Path, recursive: bool) -> list[Path]:
    """按需遍历目录内容，并跳过不关注的目录。"""
    if not recursive:
        return sorted(item for item in root.iterdir() if item.name not in SKIP_DIR_NAMES)

    items: list[Path] = []
    for item in root.rglob("*"):
        if should_skip_path(item.relative_to(root)):
            continue
        items.append(item)
    return sorted(items)


def read_text_file(workspace_root: Path, path: str, allowed_roots: list[Path] | None = None) -> tuple[Path, str]:
    """读取工作区内文本文件。"""
    target_file = resolve_workspace_path(workspace_root, path, allowed_roots)
    if not target_file.exists():
        raise ModelRetry(f"文件不存在：{path}")
    if not target_file.is_file():
        raise ModelRetry(f"目标不是文件：{path}")

    return target_file, target_file.read_text(encoding="utf-8", errors="replace")


def trim_output(text: str, max_chars: int = MAX_COMMAND_OUTPUT_CHARS) -> tuple[str, bool]:
    """限制工具输出大小，避免模型上下文被大量终端文本淹没。"""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def assert_safe_command(command: str) -> None:
    """阻止明显危险的命令，保留测试、构建、查询类命令。"""
    normalized = command.lower()
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, normalized):
            raise ValueError(f"命令包含高风险操作，已拒绝执行：{command}")


def run_subprocess(
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
) -> dict[str, object]:
    """在指定目录执行子进程并返回结构化结果。"""
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
        )
        stdout, stdout_truncated = trim_output(completed.stdout)
        stderr, stderr_truncated = trim_output(completed.stderr)
        exit_code: int | None = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = trim_output(exc.stdout or "")
        stderr, stderr_truncated = trim_output(exc.stderr or "")
        exit_code = None
        timed_out = True
        logger.warning("subprocess timed out cwd=%s timeout_seconds=%d command=%s", cwd, timeout_seconds, command)

    return {
        "cwd": cwd.as_posix(),
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "timed_out": timed_out,
    }


def get_command_shell() -> tuple[list[str], str]:
    """根据当前操作系统选择执行命令的 shell。"""
    system_name = platform.system().lower()
    if system_name == "windows":
        if shutil.which("pwsh"):
            logger.debug("selected command shell=pwsh")
            return ["pwsh", "-NoProfile", "-Command"], "pwsh"
        logger.debug("selected command shell=powershell")
        return ["powershell", "-NoProfile", "-Command"], "powershell"

    shell_path = os.environ.get("SHELL") or "/bin/sh"
    logger.debug("selected command shell=%s", Path(shell_path).name)
    return [shell_path, "-lc"], Path(shell_path).name

def check_prompt_read(ctx: object, prompt_filename: str) -> bool:
    """检查 Agent 是否已经读取过指定的系统提示词文件。"""
    if not hasattr(ctx, "messages"):
        return False
        
    messages = getattr(ctx, "messages", [])
    for msg in messages:
        parts = getattr(msg, "parts", [])
        for part in parts:
            if part.__class__.__name__ == "ToolReturnPart":
                content = str(getattr(part, "content", ""))
                # 例如返回内容中包含 path: openhachimi_agent/system_prompts/browser.md
                if prompt_filename in content:
                    return True
            elif part.__class__.__name__ == "SystemPromptPart":
                # 如果这个提示词本身就由系统主动注入(例如包含文件名的引述不代表已读，如果是全量文本注入则代表已读)
                # 我们这里主要检查 ToolReturnPart
                pass
    return False

