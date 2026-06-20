"""阶段 1:廉价工具结果剪枝(无 LLM 调用)。

在动用 LLM 摘要前先做便宜操作,大幅削减上下文:
  - 把受保护尾部之外的旧 ``ToolReturnPart.content`` 替换为一行信息性摘要
    (用配对的 ``ToolCallPart`` 的工具名 + 参数构造,如 ``[read_file] config.py (共 120 行)``)
  - 去重:同一内容多次出现只保留最新完整副本,旧的换回引
  - 尾部保护按 token 预算(从末尾向前累计),消息数作硬下限

操作原生 pydantic-ai ``ModelMessage``,返回新列表,不修改入参。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from openhachimi_agent.context.token_estimate import estimate_text_tokens

logger = logging.getLogger(__name__)

# 小于此长度的工具结果不剪枝(本身已很短)
_MIN_PRUNE_CHARS = 200
_DUPLICATE_BACKREF = "[重复工具输出 — 与更近一次调用内容相同]"

# 用于从工具参数中提取可读标识的常见键
_PATH_KEYS = ("path", "file", "file_path", "filename", "target", "dest", "destination")
_CMD_KEYS = ("command", "cmd", "command_text", "script")
_QUERY_KEYS = ("query", "pattern", "regex", "search", "text", "url")


def _content_text(content: Any) -> str:
    """把 ToolReturnPart.content(可能 str/dict/list)规范化为字符串。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        try:
            return json.dumps(content, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(content)
    return str(content)


def _content_char_len(content: Any) -> int:
    return len(_content_text(content))


def _extract_arg(args: Any, keys: tuple[str, ...]) -> str:
    """从工具参数中按优先键取出一个可读值。"""
    if not isinstance(args, dict):
        if isinstance(args, str):
            return args.strip()
        return ""
    for key in keys:
        if key in args and args[key] not in (None, ""):
            value = str(args[key]).strip()
            if value:
                return value
    return ""


def _summarize_tool_return(tool_name: str, args: Any, content: Any) -> str:
    """为一行工具结果摘要,尽量携带可读标识与体量信息。"""
    text = _content_text(content)
    char_len = len(text)
    line_count = text.count("\n") + 1 if text else 0

    detail = ""
    if tool_name in {"read_file", "read_text_file"}:
        path = _extract_arg(args, _PATH_KEYS)
        # read_file 返回结构里有 total_lines
        total_lines = ""
        if isinstance(content, dict) and content.get("total_lines"):
            total_lines = f" 共 {content['total_lines']} 行"
        detail = f"{path}{total_lines}" if path else total_lines.strip()
    elif tool_name in {"run_command", "command", "shell"}:
        cmd = _extract_arg(args, _CMD_KEYS)
        if not cmd and isinstance(content, dict):
            cmd = str(content.get("command_text") or "").strip()
        running = ""
        if isinstance(content, dict) and content.get("is_running"):
            running = " (后台运行中)"
        detail = f"{cmd}{running}" if cmd else ""
        if line_count and not running:
            detail = f"{detail} {line_count} 行输出".strip()
    elif tool_name in {"search_text", "grep", "search"}:
        query = _extract_arg(args, _QUERY_KEYS)
        detail = query or ""
    elif tool_name in {"list_files", "find_files", "list"}:
        path = _extract_arg(args, _PATH_KEYS)
        detail = path or ""
    else:
        # 通用:尝试取首个参数值
        detail = _extract_arg(args, _PATH_KEYS) or _extract_arg(args, _QUERY_KEYS)

    size_hint = f"{char_len:,} 字符"
    if detail:
        return f"[{tool_name}] {detail} ({size_hint})"
    return f"[{tool_name}] ({size_hint})"


def _build_call_id_map(messages: list[ModelMessage]) -> dict[str, tuple[str, Any]]:
    """扫描所有 ModelResponse,构建 tool_call_id -> (tool_name, args) 映射。"""
    mapping: dict[str, tuple[str, Any]] = {}
    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        for part in getattr(msg, "parts", None) or []:
            if isinstance(part, ToolCallPart) and part.tool_call_id:
                mapping[part.tool_call_id] = (part.tool_name, part.args)
    return mapping


def _find_tail_boundary(
    messages: list[ModelMessage],
    protect_tail_count: int,
    protect_tail_tokens: int | None,
) -> int:
    """从末尾向前累计 token 确定尾部保护边界,返回受保护区的起始索引。

    消息数 ``protect_tail_count`` 作为硬下限(至少保护这么多条)。
    """
    n = len(messages)
    if n == 0:
        return 0
    min_protect = min(protect_tail_count, n)
    if not protect_tail_tokens or protect_tail_tokens <= 0:
        return max(0, n - protect_tail_count)

    accumulated = 0
    boundary = n
    for i in range(n - 1, -1, -1):
        msg_tokens = estimate_text_tokens(_message_text_for_estimate(messages[i])) + 4
        protected_so_far = n - i
        if accumulated + msg_tokens > protect_tail_tokens and protected_so_far >= min_protect:
            boundary = i
            break
        accumulated += msg_tokens
        boundary = i
    # 应用消息数下限
    protected_count = n - boundary
    if protected_count < min_protect:
        protected_count = min_protect
        boundary = max(0, n - protected_count)
    return boundary


def _message_text_for_estimate(msg: ModelMessage) -> str:
    """剪枝内部用的轻量文本提取(避免与 token_estimate 模块循环依赖)。"""
    chunks: list[str] = []
    for part in getattr(msg, "parts", None) or []:
        content = getattr(part, "content", None)
        if isinstance(content, str):
            chunks.append(content)
        elif content is not None:
            chunks.append(_content_text(content))
        args = getattr(part, "args", None)
        if args is not None:
            chunks.append(args if isinstance(args, str) else _content_text(args))
    return "\n".join(chunks)


def _iter_tool_returns(msg: ModelMessage):
    """产出 (part_index, part) 中所有 ToolReturnPart。"""
    if not isinstance(msg, ModelRequest):
        return
    parts = getattr(msg, "parts", None) or []
    for idx, part in enumerate(parts):
        if isinstance(part, ToolReturnPart):
            yield idx, part


def prune_old_tool_results(
    messages: list[ModelMessage],
    *,
    protect_tail_count: int,
    protect_tail_tokens: int | None = None,
) -> tuple[list[ModelMessage], int]:
    """替换旧工具结果为一行摘要 + 去重,返回 (新消息列表, 剪枝计数)。

    尾部(最近 ``protect_tail_tokens`` token 或 ``protect_tail_count`` 条,
    取较大)内的工具结果原样保留。尾部之外的大工具结果被替换为信息性摘要;
    内容相同的重复工具结果只留最新完整副本。
    """
    if not messages:
        return list(messages), 0

    call_id_map = _build_call_id_map(messages)
    tail_boundary = _find_tail_boundary(messages, protect_tail_count, protect_tail_tokens)

    # Pass 1:去重。从末尾向前,对尾部之外的大工具结果按内容哈希去重。
    content_hashes: dict[str, int] = {}  # hash -> 第一次(最新)出现的索引
    # 用 (message_index, part_index) 标记需要换回引的位置
    backrefs: set[tuple[int, int]] = set()
    for i in range(len(messages) - 1, -1, -1):
        if i >= tail_boundary:
            continue  # 尾部不去重
        for part_idx, part in _iter_tool_returns(messages[i]):
            text = _content_text(part.content)
            if len(text) < _MIN_PRUNE_CHARS:
                continue
            h = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()[:12]
            if h in content_hashes:
                backrefs.add((i, part_idx))
            else:
                content_hashes[h] = i

    # Pass 2:构建新消息列表,替换/回引尾部之外的工具结果
    result: list[ModelMessage] = []
    pruned = 0
    for i, msg in enumerate(messages):
        if i >= tail_boundary or not isinstance(msg, ModelRequest):
            result.append(msg)
            continue
        # 处理该请求内的工具结果部件
        new_parts: list[Any] = []
        changed = False
        parts = list(getattr(msg, "parts", None) or [])
        for part_idx, part in enumerate(parts):
            if isinstance(part, ToolReturnPart) and (i, part_idx) in backrefs:
                new_parts.append(replace(part, content=_DUPLICATE_BACKREF))
                changed = True
                pruned += 1
                continue
            if isinstance(part, ToolReturnPart):
                text = _content_text(part.content)
                if len(text) >= _MIN_PRUNE_CHARS:
                    tool_name, args = call_id_map.get(part.tool_call_id or "", (part.tool_name, None))
                    summary = _summarize_tool_return(tool_name, args, part.content)
                    new_parts.append(replace(part, content=summary))
                    changed = True
                    pruned += 1
                    continue
            new_parts.append(part)
        if changed:
            result.append(replace(msg, parts=new_parts))
        else:
            result.append(msg)

    if pruned:
        logger.info("工具结果剪枝:替换/去重 %d 个旧工具结果,尾部保护边界=%d", pruned, tail_boundary)
    return result, pruned


__all__ = ["prune_old_tool_results"]
