"""工作区工具共享辅助函数。"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import logging
from pathlib import Path
from typing import TypeVar
from pydantic_ai.exceptions import ModelRetry

K = TypeVar('K')
V = TypeVar('V')

class BoundedDict(dict[K, V]):
    """带有最大容量限制的字典，当超出时移除最早插入的元素 (FIFO)。"""
    def __init__(self, max_size: int, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_size = max_size

    def __setitem__(self, key: K, value: V):
        if key not in self:
            if len(self) >= self.max_size:
                oldest_key = next(iter(self))
                del self[oldest_key]
        super().__setitem__(key, value)

MAX_LIST_ENTRIES = 200
MAX_READ_LINES = 500
MAX_READ_LINES_PER_CALL = 1000
MAX_SEARCH_RESULTS = 200
MAX_COMMAND_OUTPUT_CHARS = 12000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
SKIP_DIR_NAMES = {
    ".git",
    ".memory",
    ".tmp",
    ".tmp-ensurepip",
    ".venv",
    ".browser_data",
    ".workspace",
    "__pycache__",
    "openhachimi_agent.egg-info",
}
DANGEROUS_COMMAND_PATTERNS = [
    r"(?<!-)\brm\b",
    r"(?<!-)\bunlink\b",
    r"(?<!-)\brmdir\b",
    r"(?<!-)\bgit\s+reset\s+--hard\b",
    r"(?<!-)\bgit\s+clean\b",
    r"(?<!-)\bshutdown\b",
]

WINDOWS_DANGEROUS_COMMAND_PATTERNS = [
    r"(?<!-)\bremove-item\b",
    r"(?<!-)\bdel\b",
    r"(?<!-)\berase\b",
    r"(?<!-)\brd\b",
    r"(?:^|[;&|]\s*)(?:cmd(?:\.exe)?\s+/c\s+)?format(?:\s|$)",
    r"(?<!-)\brestart-computer\b",
    r"(?<!-)\bstop-process\b",
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


def session_workspace_dir(base_dir: Path, session_id: str) -> Path:
    """会话级临时工作区:模型自产的中间产物(一次性脚本、待发送邮件正文、临时草稿、
    本地报告)默认落点。

    - 不强制重定向(``write_file``/``run_command`` 默认行为不变),仅通过 system prompt
      引导模型把"任务过程产物"往这里写,保留"用户让我改源代码"主流用例。
    - 不自动创建(由调用方按需 ``mkdir``)。
    - 路径形如 ``<base_dir>/.workspace/<session_id>/``;``.workspace`` 已加入
      ``SKIP_DIR_NAMES``,搜索类工具不会扫;``.gitignore`` 也忽略整个目录。
    """
    return base_dir / ".workspace" / session_id


def normalize_relative_path(workspace_root: Path, path: Path) -> str:
    """将绝对路径转换为工作区相对路径。"""
    root = workspace_root.resolve()
    target = path.resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.as_posix()


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
    patterns = list(DANGEROUS_COMMAND_PATTERNS)
    if platform.system() == "Windows":
        patterns.extend(WINDOWS_DANGEROUS_COMMAND_PATTERNS)

    for pattern in patterns:
        if re.search(pattern, normalized):
            raise ModelRetry(
                f"命令包含高风险操作，已拒绝执行：{command}。\n"
                f"❌ 警告：严禁使用 shell 命令 (如 rm, del 等) 删除文件！\n"
                f"✅ 请改用专门的 `delete_path` 工具来安全地删除文件或目录。"
            )


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
    deps = getattr(ctx, "deps", None)
    if not deps:
        return False
        
    session_state = getattr(deps, "session_state", {})
    if "prompt_read_cache" not in session_state:
        session_state["prompt_read_cache"] = set()
        
    cache = session_state["prompt_read_cache"]
    if prompt_filename in cache:
        return True

    messages = getattr(ctx, "messages", [])
    if not messages:
        return False

    # 逆序遍历，通常系统提示词会在较近的交互中出现，提高查找效率
    for msg in reversed(messages):
        parts = getattr(msg, "parts", [])
        for part in parts:
            if part.__class__.__name__ == "ToolReturnPart":
                content = str(getattr(part, "content", ""))
                tool_name = str(getattr(part, "tool_name", ""))
                
                # 方案 A：通过 read_file 显式读取了该文件（检查 tool_name 与文件路径的组合）
                if tool_name == "read_file" and prompt_filename in content:
                    cache.add(prompt_filename)
                    return True
                    
                # 方案 B：由系统自动注入了该提示词
                injection_marker = f"[系统自动注入子提示词：{prompt_filename}]"
                if injection_marker in content:
                    cache.add(prompt_filename)
                    return True

            elif part.__class__.__name__ == "SystemPromptPart":
                pass
    return False

def inject_prompt_if_unread(ctx: object, prompt_name: str, original_result: str | dict[str, object]) -> str | dict[str, object]:
    """如果指定的系统提示词尚未阅读过，则加载它并将其注入到工具的返回结果中。"""
    prompt_filename = f"system_prompts/{prompt_name}.md"
    if check_prompt_read(ctx, prompt_filename):
        return original_result

    from openhachimi_agent.content.prompts import load_system_prompt
    try:
        content = load_system_prompt(prompt_name)
    except Exception as e:
        logger.warning("未能加载系统提示词 %s：%s", prompt_name, e)
        return original_result

    # 立即添加到缓存中，防止同一次 Run 中后续的工具调用重复注入
    deps = getattr(ctx, "deps", None)
    if deps:
        session_state = getattr(deps, "session_state", {})
        if "prompt_read_cache" not in session_state:
            session_state["prompt_read_cache"] = set()
        session_state["prompt_read_cache"].add(prompt_filename)

    injection = (
        f"\n\n[系统自动注入子提示词：{prompt_filename}]\n"
        f"由于你是首次使用该类型工具，请务必遵循以下操作规范：\n{content}\n\n"
        "[以上为系统自动注入的指引，以下为工具实际执行结果]\n"
    )

    if isinstance(original_result, str):
        return injection + original_result
    elif isinstance(original_result, dict):
        # 如果原始返回是字典，把注入的信息加到一个特定的字段，或者附加在主要输出字段上
        new_result = dict(original_result)
        # 如果字典中有 "output" 字段（如命令工具或浏览器状态）
        if "output" in new_result and isinstance(new_result["output"], str):
            new_result["output"] = injection + new_result["output"]
        elif "message" in new_result and isinstance(new_result["message"], str):
            new_result["message"] = injection + new_result["message"]
        else:
            new_result["_system_instruction"] = injection.strip()
        return new_result
        
    return original_result

