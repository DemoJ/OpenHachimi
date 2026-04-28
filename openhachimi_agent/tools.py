"""基于 PydanticAI FunctionToolset 的工作区工具。"""

from __future__ import annotations

import fnmatch

from pydantic_ai import FunctionToolset, RunContext

from openhachimi_agent.config import AppConfig
from openhachimi_agent.tool_utils import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    MAX_LIST_ENTRIES,
    MAX_READ_LINES,
    MAX_SEARCH_RESULTS,
    assert_safe_command,
    get_command_shell,
    iter_workspace_items,
    normalize_relative_path,
    read_text_file,
    relative_path_from,
    resolve_workspace_path,
    run_subprocess,
)


def list_files(
    ctx: RunContext[AppConfig],
    path: str = ".",
    recursive: bool = False,
    max_entries: int = MAX_LIST_ENTRIES,
) -> dict[str, object]:
    """列出工作区内某个目录下的文件和子目录。"""
    target_dir = resolve_workspace_path(ctx.deps.base_dir, path)
    if not target_dir.exists():
        raise FileNotFoundError(f"目录不存在：{path}")
    if not target_dir.is_dir():
        raise NotADirectoryError(f"目标不是目录：{path}")

    max_entries = max(1, min(max_entries, MAX_LIST_ENTRIES))
    entries: list[dict[str, object]] = []

    for item in iter_workspace_items(target_dir, recursive):
        entries.append(
            {
                "path": normalize_relative_path(ctx.deps.base_dir, item),
                "type": "directory" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None,
            }
        )
        if len(entries) >= max_entries:
            break

    return {
        "directory": normalize_relative_path(ctx.deps.base_dir, target_dir)
        if target_dir != ctx.deps.base_dir
        else ".",
        "recursive": recursive,
        "entries": entries,
        "truncated": len(entries) >= max_entries,
    }


def find_files(
    ctx: RunContext[AppConfig],
    pattern: str,
    path: str = ".",
    max_entries: int = MAX_SEARCH_RESULTS,
) -> dict[str, object]:
    """按 glob 模式在工作区中查找文件和目录。"""
    target_dir = resolve_workspace_path(ctx.deps.base_dir, path)
    if not target_dir.exists():
        raise FileNotFoundError(f"目录不存在：{path}")
    if not target_dir.is_dir():
        raise NotADirectoryError(f"目标不是目录：{path}")

    max_entries = max(1, min(max_entries, MAX_SEARCH_RESULTS))
    matches: list[dict[str, object]] = []
    for item in iter_workspace_items(target_dir, recursive=True):
        relative_path = normalize_relative_path(ctx.deps.base_dir, item)
        if fnmatch.fnmatch(item.name, pattern) or fnmatch.fnmatch(relative_path, pattern):
            matches.append(
                {
                    "path": relative_path,
                    "type": "directory" if item.is_dir() else "file",
                }
            )
        if len(matches) >= max_entries:
            break

    return {
        "pattern": pattern,
        "directory": normalize_relative_path(ctx.deps.base_dir, target_dir)
        if target_dir != ctx.deps.base_dir
        else ".",
        "matches": matches,
        "truncated": len(matches) >= max_entries,
    }


def search_text(
    ctx: RunContext[AppConfig],
    query: str,
    path: str = ".",
    file_pattern: str = "*",
    case_sensitive: bool = False,
    max_results: int = MAX_SEARCH_RESULTS,
) -> dict[str, object]:
    """在工作区文本文件中搜索指定字符串。"""
    if not query:
        raise ValueError("query 不能为空")

    target_dir = resolve_workspace_path(ctx.deps.base_dir, path)
    if not target_dir.exists():
        raise FileNotFoundError(f"目录不存在：{path}")
    if not target_dir.is_dir():
        raise NotADirectoryError(f"目标不是目录：{path}")

    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
    normalized_query = query if case_sensitive else query.lower()
    matches: list[dict[str, object]] = []

    for item in iter_workspace_items(target_dir, recursive=True):
        if item.is_dir():
            continue

        relative_path = normalize_relative_path(ctx.deps.base_dir, item)
        if not (fnmatch.fnmatch(item.name, file_pattern) or fnmatch.fnmatch(relative_path, file_pattern)):
            continue

        text = item.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            haystack = line if case_sensitive else line.lower()
            if normalized_query in haystack:
                matches.append(
                    {
                        "path": relative_path,
                        "line": line_number,
                        "text": line,
                    }
                )
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break

    return {
        "query": query,
        "directory": normalize_relative_path(ctx.deps.base_dir, target_dir)
        if target_dir != ctx.deps.base_dir
        else ".",
        "file_pattern": file_pattern,
        "matches": matches,
        "truncated": len(matches) >= max_results,
    }


def read_file(
    ctx: RunContext[AppConfig],
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict[str, object]:
    """读取工作区内文件的全部内容或指定行范围。"""
    target_file, text = read_text_file(ctx.deps.base_dir, path)
    if start_line < 1:
        raise ValueError("start_line 必须大于等于 1")

    lines = text.splitlines()
    total_lines = len(lines)

    if end_line is None:
        end_line = min(total_lines, start_line + MAX_READ_LINES - 1)
    if end_line < start_line:
        raise ValueError("end_line 不能小于 start_line")

    selected = lines[start_line - 1 : end_line]
    numbered_content = "\n".join(
        f"{index}: {line}" for index, line in enumerate(selected, start=start_line)
    )

    return {
        "path": normalize_relative_path(ctx.deps.base_dir, target_file),
        "start_line": start_line,
        "end_line": start_line + len(selected) - 1 if selected else start_line - 1,
        "total_lines": total_lines,
        "content": numbered_content,
    }


def write_file(
    ctx: RunContext[AppConfig],
    path: str,
    content: str,
    overwrite: bool = True,
) -> dict[str, object]:
    """在工作区内写入文件内容，可用于新建或覆盖文件。"""
    target_file = resolve_workspace_path(ctx.deps.base_dir, path)
    existed_before = target_file.exists()
    if target_file.exists() and target_file.is_dir():
        raise IsADirectoryError(f"目标是目录，不能直接写入：{path}")
    if target_file.exists() and not overwrite:
        raise FileExistsError(f"文件已存在，且 overwrite=False：{path}")

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(content, encoding="utf-8")

    return {
        "path": normalize_relative_path(ctx.deps.base_dir, target_file),
        "bytes_written": len(content.encode("utf-8")),
        "overwritten": existed_before,
    }


def make_directory(
    ctx: RunContext[AppConfig],
    path: str,
    parents: bool = True,
    exist_ok: bool = True,
) -> dict[str, object]:
    """在工作区内创建目录。"""
    target_dir = resolve_workspace_path(ctx.deps.base_dir, path)
    existed_before = target_dir.exists()
    if existed_before and not target_dir.is_dir():
        raise NotADirectoryError(f"目标已存在且不是目录：{path}")

    target_dir.mkdir(parents=parents, exist_ok=exist_ok)

    return {
        "path": normalize_relative_path(ctx.deps.base_dir, target_dir),
        "created": not existed_before,
    }


def replace_in_file(
    ctx: RunContext[AppConfig],
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> dict[str, object]:
    """在工作区文件中替换指定文本片段。"""
    if not old_text:
        raise ValueError("old_text 不能为空")

    target_file, original_text = read_text_file(ctx.deps.base_dir, path)
    match_count = original_text.count(old_text)
    if match_count == 0:
        raise ValueError("未找到需要替换的文本片段")
    if match_count > 1 and not replace_all:
        raise ValueError("匹配到多个位置，请将 replace_all 设为 true 后重试")

    updated_text = (
        original_text.replace(old_text, new_text)
        if replace_all
        else original_text.replace(old_text, new_text, 1)
    )
    target_file.write_text(updated_text, encoding="utf-8")

    return {
        "path": normalize_relative_path(ctx.deps.base_dir, target_file),
        "replacements": match_count if replace_all else 1,
    }


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
    return result


def git_status(ctx: RunContext[AppConfig], cwd: str = ".") -> dict[str, object]:
    """查看当前工作区的 Git 状态。"""
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


WORKSPACE_TOOLSET = FunctionToolset(
    tools=[
        list_files,
        find_files,
        search_text,
        read_file,
        write_file,
        make_directory,
        replace_in_file,
        run_command,
        git_status,
        git_diff,
    ]
)
