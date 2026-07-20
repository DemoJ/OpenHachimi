"""工具执行权限控制。

提供两种权限模式:
- blacklist: 黑名单模式(默认)。危险命令匹配内置黑名单时,通过 clarify_user 询问用户确认后才执行。
- allow_all: 完全允许模式。所有命令直接放行,不询问。

权限配置可在 config.yaml 的 permission.mode 中随时修改,新会话生效。
黑名单文件固定为 user/permission-blacklist.json,与内置黑名单合并。
JSON 格式: {"dangerous_patterns": ["regex1", "regex2", ...]}
"""

from __future__ import annotations

import json
import logging
import platform
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 内置危险命令黑名单(与 tools/utils.py 中的 assert_safe_command 保持一致,
# 但这里用于"询问后放行"而非"直接拒绝")。
_BUILTIN_DANGEROUS_COMMAND_PATTERNS = [
    r"(?<!-)\brm\b",
    r"(?<!-)\bunlink\b",
    r"(?<!-)\brmdir\b",
    r"(?<!-)\bgit\s+reset\s+--hard\b",
    r"(?<!-)\bgit\s+clean\b",
    r"(?<!-)\bshutdown\b",
]

_BUILTIN_WINDOWS_DANGEROUS_COMMAND_PATTERNS = [
    r"(?<!-)\bremove-item\b",
    r"(?<!-)\bdel\b",
    r"(?<!-)\berase\b",
    r"(?<!-)\brd\b",
    r"(?:^|[;&|]\s*)(?:cmd(?:\.exe)?\s+/c\s+)?format(?:\s|$)",
    r"(?<!-)\brestart-computer\b",
    r"(?<!-)\bstop-process\b",
]

# 用户自定义黑名单文件固定路径(相对项目根)。
BLACKLIST_FILE_RELATIVE_PATH = "user/permission-blacklist.json"


def _load_custom_patterns(base_dir: Path) -> list[str]:
    """从固定路径 JSON 文件加载用户自定义黑名单正则表达式。

    文件不存在或格式错误时返回空列表,不阻断启动。
    """
    path = base_dir / BLACKLIST_FILE_RELATIVE_PATH
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        patterns = data.get("dangerous_patterns", [])
        if not isinstance(patterns, list):
            logger.warning("permission blacklist file invalid format (dangerous_patterns must be list): %s", path)
            return []
        return [str(p) for p in patterns if isinstance(p, str) and p.strip()]
    except Exception as exc:
        logger.warning("failed to load permission blacklist file %s: %s", path, exc)
        return []


def is_dangerous_command(command: str, base_dir: Path | None = None) -> bool:
    """判断命令是否命中危险黑名单(内置 + 用户自定义 JSON 文件)。

    参数:
        command: 待检查的命令字符串
        base_dir: 项目根目录,用于定位 user/permission-blacklist.json
    """
    normalized = command.lower()
    patterns = list(_BUILTIN_DANGEROUS_COMMAND_PATTERNS)
    if platform.system() == "Windows":
        patterns.extend(_BUILTIN_WINDOWS_DANGEROUS_COMMAND_PATTERNS)

    if base_dir:
        patterns.extend(_load_custom_patterns(base_dir))

    return any(re.search(pattern, normalized) for pattern in patterns)


def read_blacklist_file(base_dir: Path) -> dict:
    """读取黑名单 JSON 文件内容,供 WebUI 编辑。"""
    path = base_dir / BLACKLIST_FILE_RELATIVE_PATH
    if not path.exists():
        return {"dangerous_patterns": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"dangerous_patterns": []}


def write_blacklist_file(base_dir: Path, patterns: list[str]) -> None:
    """写入黑名单 JSON 文件。"""
    path = base_dir / BLACKLIST_FILE_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"dangerous_patterns": patterns}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
