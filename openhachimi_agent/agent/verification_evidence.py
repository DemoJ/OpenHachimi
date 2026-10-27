"""Tool effects and verification evidence, scoped to workspace or browser state.

Browser navigation/view changes are not code edits. Only meaningful browser
mutations require browser evidence; local reads and Git checks cannot verify them.
These defaults apply to successful legacy tools. Explicit outcomes can report
partial effects on failure, or override defaults for a no-op.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal


VerificationScope = Literal["workspace", "browser"]

# Only these tools can dirty local workspace verification.
_WORKSPACE_EDIT_TOOLS: frozenset[str] = frozenset(
    {
        # 文件写入/替换/删除
        "write_file",
        "replace_in_file",
        "make_directory",
        "delete_path",
        "publish_artifact",
        # 命令执行(可能改文件、装依赖、跑构建)
        "run_command",
        "send_command_input",
        # skill 安装会落盘文件
        "install_skill",
    }
)


# ── 纯文本/文档后缀:编辑这些不触发验证 nudge ─────────────────────────────
# 照搬 Hermes verification_stop._NON_CODE_VERIFY_EXTENSIONS:文档/散文/数据标记
# 没有可验证的运行时行为,改完不需要跑测试/lint。避免"改了个 README 就被 nudge
# 去跑验证"的误报。
_NON_CODE_VERIFY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md",
        ".markdown",
        ".mdx",
        ".rst",
        ".txt",
        ".text",
        ".adoc",
        ".asciidoc",
        ".org",
        ".log",
        ".csv",
        ".tsv",
    }
)

# 纯文本文件名(无后缀或后缀不在上面集合):LICENSE / CHANGELOG 等。
_NON_CODE_VERIFY_FILENAMES: frozenset[str] = frozenset(
    {
        "license",
        "licence",
        "notice",
        "authors",
        "contributors",
        "changelog",
        "codeowners",
    }
)

# 会带文件路径参数的工具——mark_tool_succeeded 据此从 bound_args 里抽路径做后缀判断。
# 路径参数名因工具而异,这里列已知;未列出的工具读不到路径时保守置 stale(不误放)。
_PATH_ARG_NAMES: tuple[str, ...] = ("file_path", "path", "target")


def is_non_code_path(raw: str | None) -> bool:
    """该路径是否为文档/散文/数据标记(改了也不需要跑验证)。

    照搬 Hermes:看后缀是否在纯文本集合,或无后缀时看文件名是否为 LICENSE/CHANGELOG 等。
    解析失败返回 False(保守认为需要验证)。
    """
    if not raw:
        return False
    try:
        p = Path(str(raw))
    except Exception:
        return False
    suffix = p.suffix.lower()
    if suffix in _NON_CODE_VERIFY_EXTENSIONS:
        return True
    if not suffix and p.name.lower() in _NON_CODE_VERIFY_FILENAMES:
        return True
    return False


# Click/type/submit/select may change remote state, unlike reading, navigating,
# scrolling, hovering or managing tabs. A typed outcome may refine a no-op.
_BROWSER_EDIT_TOOLS: frozenset[str] = frozenset(
    {"browser_click", "browser_type", "browser_press_key", "browser_select_option"}
)

# Preserve the existing workspace self-check policy, but never cross domains.
_WORKSPACE_VERIFY_TOOLS: frozenset[str] = frozenset(
    {
        "run_command",
        "send_command_input",
        "git_status",
        "git_diff",
        "read_file",
        "list_files",
        "find_files",
        "search_text",
        "command_status",
    }
)
_BROWSER_VERIFY_TOOLS: frozenset[str] = frozenset(
    {"browser_get_state", "browser_extract_content"}
)


def effect_scopes(tool_name: str | None) -> frozenset[VerificationScope]:
    """Default effects of a successful tool; failures have no inferred effects."""
    if tool_name in _WORKSPACE_EDIT_TOOLS:
        return frozenset({"workspace"})
    if tool_name in _BROWSER_EDIT_TOOLS:
        return frozenset({"browser"})
    return frozenset()


def evidence_scopes(tool_name: str | None) -> frozenset[VerificationScope]:
    """Only observations of the matching domain can clear its verification gap."""
    if tool_name in _WORKSPACE_VERIFY_TOOLS:
        return frozenset({"workspace"})
    if tool_name in _BROWSER_VERIFY_TOOLS:
        return frozenset({"browser"})
    return frozenset()


@lru_cache(maxsize=1)
def edit_tools() -> frozenset[str]:
    return _WORKSPACE_EDIT_TOOLS | _BROWSER_EDIT_TOOLS


@lru_cache(maxsize=1)
def verify_tools() -> frozenset[str]:
    return _WORKSPACE_VERIFY_TOOLS | _BROWSER_VERIFY_TOOLS


def is_edit_tool(tool_name: str | None) -> bool:
    """Whether a successful call can leave a scoped verification gap."""
    return bool(effect_scopes(tool_name))


def is_verify_tool(tool_name: str | None) -> bool:
    return bool(evidence_scopes(tool_name))


__all__ = [
    "VerificationScope",
    "effect_scopes",
    "evidence_scopes",
    "edit_tools",
    "verify_tools",
    "is_edit_tool",
    "is_verify_tool",
    "is_non_code_path",
]
