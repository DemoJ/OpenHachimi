"""工作区文件发现、搜索和读取工具。"""

from __future__ import annotations

import fnmatch
import logging

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.utils import (
    MAX_LIST_ENTRIES,
    MAX_READ_LINES,
    MAX_READ_LINES_PER_CALL,
    MAX_SEARCH_RESULTS,
    iter_workspace_items,
    normalize_relative_path,
    read_text_file,
    resolve_workspace_path,
)


logger = logging.getLogger(__name__)


def list_files(
    ctx: RunContext[AgentDeps],
    path: str = ".",
    recursive: bool = False,
    max_entries: int = MAX_LIST_ENTRIES,
) -> dict[str, object]:
    """列出工作区内某个目录下的文件和子目录。"""
    target_dir = resolve_workspace_path(ctx.deps.base_dir, path, ctx.deps.skills_dirs)
    logger.debug("tool list_files path=%s recursive=%s max_entries=%d", path, recursive, max_entries)
    if not target_dir.exists():
        raise ModelRetry(f"目录不存在：{path}")
    if not target_dir.is_dir():
        raise ModelRetry(f"目标不是目录：{path}")

    max_entries = max(1, min(max_entries, MAX_LIST_ENTRIES))
    entries: list[dict[str, object]] = []

    for item in iter_workspace_items(target_dir, recursive):
        try:
            size = item.stat().st_size if item.is_file() else None
        except Exception:
            size = None
            
        entries.append(
            {
                "path": normalize_relative_path(ctx.deps.base_dir, item),
                "type": "directory" if item.is_dir() else "file",
                "size": size,
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
    ctx: RunContext[AgentDeps],
    pattern: str,
    path: str = ".",
    max_entries: int = MAX_SEARCH_RESULTS,
) -> dict[str, object]:
    """按 glob 模式在工作区中查找文件和目录。"""
    target_dir = resolve_workspace_path(ctx.deps.base_dir, path, ctx.deps.skills_dirs)
    logger.debug("tool find_files pattern=%s path=%s max_entries=%d", pattern, path, max_entries)
    if not target_dir.exists():
        raise ModelRetry(f"目录不存在：{path}")
    if not target_dir.is_dir():
        raise ModelRetry(f"目标不是目录：{path}")

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
    ctx: RunContext[AgentDeps],
    query: str,
    path: str = ".",
    file_pattern: str = "*",
    case_sensitive: bool = False,
    max_results: int = MAX_SEARCH_RESULTS,
) -> dict[str, object]:
    """在工作区文本文件中搜索指定字符串。"""
    if not query:
        raise ModelRetry("query 不能为空")

    target_dir = resolve_workspace_path(ctx.deps.base_dir, path, ctx.deps.skills_dirs)
    logger.debug("tool search_text path=%s file_pattern=%s case_sensitive=%s", path, file_pattern, case_sensitive)
    if not target_dir.exists():
        raise ModelRetry(f"目录不存在：{path}")
    if not target_dir.is_dir():
        raise ModelRetry(f"目标不是目录：{path}")

    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
    normalized_query = query if case_sensitive else query.lower()
    matches: list[dict[str, object]] = []

    for item in iter_workspace_items(target_dir, recursive=True):
        if item.is_dir():
            continue

        relative_path = normalize_relative_path(ctx.deps.base_dir, item)
        if not (fnmatch.fnmatch(item.name, file_pattern) or fnmatch.fnmatch(relative_path, file_pattern)):
            continue

        try:
            text = item.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.debug("读取文件 %s 失败：%s", item, e)
            continue

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
    ctx: RunContext[AgentDeps],
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict[str, object]:
    """读取工作区内文件的全部内容或指定行范围。"""
    logger.debug("tool read_file path=%s start_line=%d end_line=%s", path, start_line, end_line)
    target_file, text = read_text_file(ctx.deps.base_dir, path)
    if start_line < 1:
        raise ModelRetry("start_line 必须大于等于 1")

    lines = text.splitlines()
    total_lines = len(lines)

    if end_line is None:
        requested_end_line = min(total_lines, start_line + MAX_READ_LINES - 1)
    else:
        requested_end_line = end_line
    if requested_end_line < start_line:
        raise ModelRetry("end_line 不能小于 start_line")

    effective_end_line = min(requested_end_line, start_line + MAX_READ_LINES_PER_CALL - 1)
    selected = lines[start_line - 1 : effective_end_line]
    actual_end_line = start_line + len(selected) - 1 if selected else start_line - 1
    truncated = requested_end_line > effective_end_line or actual_end_line < total_lines
    next_start_line = actual_end_line + 1 if truncated and actual_end_line < total_lines else None
    numbered_content = "\n".join(
        f"{index}: {line}" for index, line in enumerate(selected, start=start_line)
    )

    return {
        "path": normalize_relative_path(ctx.deps.base_dir, target_file),
        "start_line": start_line,
        "end_line": actual_end_line,
        "total_lines": total_lines,
        "truncated": truncated,
        "next_start_line": next_start_line,
        "content": numbered_content,
    }
