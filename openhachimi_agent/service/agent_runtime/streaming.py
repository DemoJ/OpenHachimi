"""Streaming event bridge and queue consumption helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.service.agent_runtime.context import OperationState


logger = logging.getLogger(__name__)
STREAM_DONE = object()
REDACTED = "[REDACTED]"
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "passwd",
    "password",
    "private_key",
    "secret",
    "token",
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|cookie)\s*[:=]\s*([^\s'\";&]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
)


@dataclass
class StreamEventItem:
    type: Literal["text", "tool", "system"]
    text: str
    tool_name: str | None = None
    tool_icon: str | None = None
    temporary: bool = False
    counted_as_output: bool = True


@dataclass
class StreamStats:
    output_chars: int = 0
    chunk_count: int = 0
    first_chunk_ms: float | None = None
    last_chunk_preview: str = ""


class OperationStalledError(TimeoutError):
    def __init__(self, operation: str, stalled_for: float, timeout: int) -> None:
        self.operation = operation
        self.stalled_for = stalled_for
        self.timeout = timeout
        super().__init__(
            f"当前操作 {operation} 已 {stalled_for:.0f}s 没有可验证进展，超过 watchdog 阈值 {timeout}s。"
        )


def text_from_stream_event(event: object) -> str:
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    return ""


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _redact_string(text: str) -> str:
    redacted = text
    for pattern in SENSITIVE_VALUE_PATTERNS:
        if pattern.pattern.startswith("(?i)\\b(Bearer"):
            redacted = pattern.sub(r"\1" + REDACTED, redacted)
        elif "api" in pattern.pattern and "authorization" in pattern.pattern:
            redacted = pattern.sub(lambda match: f"{match.group(1)}={REDACTED}", redacted)
        else:
            redacted = pattern.sub(REDACTED, redacted)
    return redacted


def redact_tool_args(args: object) -> object:
    if isinstance(args, dict):
        return {
            key: REDACTED if _is_sensitive_key(key) else redact_tool_args(value)
            for key, value in args.items()
        }
    if isinstance(args, list):
        return [redact_tool_args(item) for item in args]
    if isinstance(args, tuple):
        return tuple(redact_tool_args(item) for item in args)
    if isinstance(args, str):
        return _redact_string(args)
    return args


def summarize_tool_args(args: object, max_chars: int = 160) -> str:
    args = redact_tool_args(args)
    if args in (None, "", {}):
        return ""
    if isinstance(args, str):
        text = args
    else:
        import json

        try:
            text = json.dumps(args, ensure_ascii=False)
        except TypeError:
            text = str(args)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def tool_icon_for_name(tool_name: str) -> str:
    if tool_name.startswith("browser_"):
        return "🌐"
    if tool_name in {"run_command", "send_command_input", "command_status"}:
        return "🖥️"
    if tool_name in {"write_file", "make_directory", "replace_in_file", "delete_path"}:
        return "✏️"
    if tool_name in {"list_files", "find_files", "search_text", "read_file"}:
        return "📄"
    if tool_name.startswith("git_"):
        return "🌿"
    if tool_name in {"web_fetch", "web_search", "discover_web_resources"}:
        return "🔎"
    if tool_name in {"create_todos", "update_todo", "get_todos"}:
        return "✅"
    if "skill" in tool_name:
        return "🧩"
    return "🔧"


def _compact(value: object, max_chars: int = 80) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _value(args: dict[str, object], key: str, default: object = "") -> object:
    value = args.get(key, default)
    return default if value is None else value


def _task_description(item: object) -> str:
    if isinstance(item, str):
        return _compact(item, 90)
    if isinstance(item, dict):
        desc = _compact(item.get("description", "Unnamed Task"), 90)
        task_id = item.get("id")
        prefix = f"{task_id}. " if task_id not in (None, "") else ""
        criteria = _compact(item.get("success_criteria", ""), 60)
        return f"{prefix}{desc}" + (f"（验收：{criteria}）" if criteria else "")
    return _compact(item, 90)


def _tasks_summary(tasks: object, max_items: int = 4) -> str:
    if not isinstance(tasks, list):
        return _compact(tasks, 160)
    items = [_task_description(item) for item in tasks[:max_items]]
    suffix = f" 等 {len(tasks)} 项" if len(tasks) > max_items else ""
    return "；".join(item for item in items if item) + suffix


def _tool_detail(tool_name: str, args: dict[str, object]) -> str:
    if not args:
        return ""

    if tool_name == "create_todos":
        goal = _compact(args.get("goal", ""), 60)
        tasks = args.get("tasks", [])
        task_count = len(tasks) if isinstance(tasks, list) else 0
        detail_parts = []
        if goal:
            detail_parts.append(f"目标：{goal}")
        if task_count:
            detail_parts.append(f"共 {task_count} 项任务")
        return "；".join(detail_parts)

    if tool_name == "update_todo":
        task_id = _value(args, "task_id")
        status = _value(args, "status")
        notes = _compact(args.get("notes", ""), 500)
        evidence = _compact(args.get("evidence", ""), 500)
        detail = f"任务 {task_id} → {status}" if task_id else f"状态：{status}"
        if notes:
            detail += f"；备注：{notes}"
        if evidence:
            detail += f"；证据：{evidence}"
        return detail

    if tool_name == "get_todos":
        return "查看当前计划进度"

    if tool_name == "read_file":
        path = _compact(_value(args, "path"), 120)
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        line_range = ""
        if start_line and end_line:
            line_range = f"：第 {start_line}-{end_line} 行"
        elif start_line:
            line_range = f"：从第 {start_line} 行开始"
        return f"读取 {path}{line_range}" if path else ""

    if tool_name in {"write_file", "replace_in_file", "delete_path", "make_directory"}:
        path = _compact(_value(args, "path"), 120)
        if tool_name == "write_file":
            content = args.get("content", "")
            size = len(str(content).encode("utf-8")) if content is not None else 0
            overwrite = _value(args, "overwrite", True)
            return f"写入 {path}（{size} bytes，overwrite={overwrite}）" if path else ""
        if tool_name == "replace_in_file":
            old_text = _compact(args.get("old_text", ""), 60)
            new_text = _compact(args.get("new_text", ""), 60)
            replace_all = _value(args, "replace_all", False)
            return f"替换 {path}：{old_text} → {new_text}（replace_all={replace_all}）" if path else ""
        if tool_name == "make_directory":
            return f"创建目录 {path}" if path else ""
        return f"删除 {path}" if path else ""

    if tool_name in {"list_files", "find_files", "search_text"}:
        path = _compact(_value(args, "path", "."), 100)
        if tool_name == "list_files":
            recursive = _value(args, "recursive", False)
            return f"列出 {path}（recursive={recursive}）"
        if tool_name == "find_files":
            pattern = _compact(_value(args, "pattern"), 80)
            return f"在 {path} 查找 {pattern}"
        query = _compact(_value(args, "query"), 80)
        file_pattern = _compact(_value(args, "file_pattern", "*"), 60)
        return f"在 {path} 搜索 {query}（{file_pattern}）"

    if tool_name in {"run_command", "command_status", "send_command_input"}:
        if tool_name == "run_command":
            command = _compact(_value(args, "command"), 140)
            cwd = _compact(_value(args, "cwd", "."), 80)
            return f"{command}（cwd={cwd}）" if command else ""
        command_id = _compact(_value(args, "command_id"), 80)
        if tool_name == "send_command_input":
            special_key = _value(args, "special_key", "none")
            text = _compact(args.get("text", ""), 80)
            return f"向命令 {command_id} 输入 {text or special_key}"
        return f"检查命令 {command_id} 状态"

    if tool_name.startswith("browser_"):
        if "url" in args:
            return f"URL：{_compact(args['url'], 140)}"
        if "element_id" in args and "text" in args:
            return f"元素 #{args['element_id']} 输入：{_compact(args['text'], 80)}"
        if "element_id" in args:
            return f"元素 #{args['element_id']}"
        if "direction" in args:
            amount = _value(args, "amount", 600)
            element_id = args.get("element_id")
            target = f"，元素 #{element_id}" if element_id is not None else ""
            return f"{args['direction']} {amount}px{target}"
        if "tab_index" in args:
            return f"标签页 #{args['tab_index']}"
        return "获取页面状态" if tool_name == "browser_get_state" else ""

    if tool_name in {"web_fetch", "discover_web_resources"}:
        return f"URL：{_compact(_value(args, 'url'), 140)}"

    if tool_name == "web_search":
        return f"关键词：{_compact(_value(args, 'query'), 140)}"

    if tool_name.startswith("git_"):
        if tool_name == "git_diff":
            path = _compact(_value(args, "path", ""), 100)
            ref = _compact(_value(args, "ref", ""), 80)
            staged = _value(args, "staged", False)
            parts = [item for item in [f"path={path}" if path else "", f"ref={ref}" if ref else "", f"staged={staged}"] if item]
            return "，".join(parts)
        return f"cwd={_compact(_value(args, 'cwd', '.'), 80)}"

    return summarize_tool_args(args)


def _tool_action(tool_name: str) -> str:
    actions = {
        "create_todos": "创建计划",
        "update_todo": "更新计划",
        "get_todos": "查看计划",
        "read_file": "读取文件",
        "write_file": "写入文件",
        "replace_in_file": "替换文件",
        "delete_path": "删除路径",
        "make_directory": "创建目录",
        "list_files": "列出文件",
        "find_files": "查找文件",
        "search_text": "搜索文本",
        "run_command": "执行命令",
        "command_status": "检查命令",
        "send_command_input": "发送输入",
        "browser_navigate": "打开网页",
        "browser_get_state": "查看页面",
        "browser_click": "点击页面",
        "browser_type": "输入文本",
        "browser_scroll": "滚动页面",
        "browser_list_tabs": "查看标签页",
        "browser_new_tab": "新建标签页",
        "browser_switch_tab": "切换标签页",
        "browser_close_tab": "关闭标签页",
        "web_fetch": "抓取网页",
        "discover_web_resources": "发现网页资源",
        "web_search": "搜索网页",
        "git_status": "查看 Git 状态",
        "git_diff": "查看 Git 差异",
        "list_skills": "查看技能",
        "get_skill_instructions": "读取技能说明",
        "install_skill": "安装技能",
    }
    return actions.get(tool_name, tool_name)


def format_tool_call(tool_name: str, args: dict[str, object]) -> str:
    safe_args = redact_tool_args(args)
    if not isinstance(safe_args, dict):
        safe_args = {}
    icon = tool_icon_for_name(tool_name)
    action = _tool_action(tool_name)
    detail = _tool_detail(tool_name, safe_args)
    return f"{icon} {action}：{detail}" if detail else f"{icon} {action}"


def event_item_from_stream_event(event: object) -> StreamEventItem | None:
    if chunk := text_from_stream_event(event):
        return StreamEventItem(type="text", text=chunk)

    if isinstance(event, FunctionToolCallEvent):
        tool_name = event.part.tool_name
        args_dict = event.part.args_as_dict()
        args = summarize_tool_args(args_dict)
        logger.info(
            "tool stream call tool_name=%s args=%s",
            tool_name,
            args,
        )
        return StreamEventItem(
            type="tool",
            text=format_tool_call(tool_name, args_dict),
            tool_name=tool_name,
            tool_icon=tool_icon_for_name(tool_name),
            temporary=True,
            counted_as_output=False,
        )

    if isinstance(event, FunctionToolResultEvent):
        result = event.result
        outcome = getattr(result, "outcome", "success")
        logger.info(
            "tool stream result tool_name=%s outcome=%s",
            result.tool_name,
            outcome,
        )
        return None

    return None


def _update_operation_from_event(operation_state: OperationState, event: object) -> None:
    if isinstance(event, FunctionToolCallEvent):
        operation_state.start("tool", event.part.tool_name, summarize_tool_args(event.part.args_as_dict()))
        return
    if isinstance(event, FunctionToolResultEvent):
        operation_state.progress()
        operation_state.start("model", "executor")
        return
    if text_from_stream_event(event):
        operation_state.progress()


def build_stream_event_handler(
    stream_queue: asyncio.Queue[StreamEventItem | object],
    operation_state: OperationState,
) -> Callable[[object, object], Awaitable[None]]:
    async def handle_stream_events(_ctx: object, stream_event: object) -> None:
        async for event in stream_event:  # type: ignore[attr-defined]
            _update_operation_from_event(operation_state, event)
            if item := event_item_from_stream_event(event):
                await stream_queue.put(item)

    return handle_stream_events


def _operation_timeout_seconds(operation_state: OperationState, config: AppConfig) -> int:
    name = operation_state.name or ""
    if operation_state.kind == "planner":
        return max(240, config.agent_timeout_seconds)
    if operation_state.kind == "model":
        return max(180, config.agent_timeout_seconds)
    if name.startswith("browser_"):
        return 120
    if name in {"web_fetch", "web_search", "discover_web_resources"}:
        return 90
    if name in {"read_file", "list_files", "find_files", "search_text", "git_status", "git_diff"}:
        return 120
    if name in {"run_command", "send_command_input", "command_status"}:
        return 60
    return max(120, config.stream_idle_timeout_seconds * 3)


def _stalled_for(operation_state: OperationState) -> float:
    return time.perf_counter() - operation_state.last_progress_at


def _raise_if_operation_stalled(operation_state: OperationState, config: AppConfig) -> None:
    timeout = _operation_timeout_seconds(operation_state, config)
    stalled_for = _stalled_for(operation_state)
    if stalled_for > timeout:
        raise OperationStalledError(operation_state.describe(), stalled_for, timeout)



async def _cancel_stalled_task(task: asyncio.Task, exc: OperationStalledError) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    logger.error(
        "chat operation stalled operation=%s stalled_for=%.0f timeout=%d",
        exc.operation,
        exc.stalled_for,
        exc.timeout,
    )


def system_stream_event(text: str) -> StreamEventItem:
    return StreamEventItem(type="system", text=text)


async def consume_stream_queue(
    *,
    stream_queue: asyncio.Queue[StreamEventItem | object],
    task: asyncio.Task,
    config: AppConfig,
    role: str,
    session_id: str,
    start_time: float,
    stats: StreamStats,
    operation_state: OperationState,
) -> AsyncIterator[StreamEventItem]:
    while True:
        try:
            item = await asyncio.wait_for(
                stream_queue.get(),
                timeout=config.stream_idle_timeout_seconds,
            )
        except asyncio.TimeoutError:
            if task.done():
                break
            try:
                _raise_if_operation_stalled(operation_state, config)
            except OperationStalledError as exc:
                await _cancel_stalled_task(task, exc)
                raise
            operation_state.heartbeat_count += 1
            logger.info(
                "chat heartbeat role=%s session_id=%s operation=%s stalled_for=%.0f detail=%s",
                role,
                session_id,
                operation_state.describe(),
                _stalled_for(operation_state),
                operation_state.detail or "",
            )
            continue

        if item is STREAM_DONE:
            break

        if not isinstance(item, StreamEventItem):
            continue

        try:
            _raise_if_operation_stalled(operation_state, config)
        except OperationStalledError as exc:
            await _cancel_stalled_task(task, exc)
            raise

        chunk = item.text
        compact_chunk = " ".join(chunk.split())
        if len(compact_chunk) > 120:
            compact_chunk = compact_chunk[:117] + "..."
        if compact_chunk:
            stats.last_chunk_preview = compact_chunk
        if stats.first_chunk_ms is None:
            stats.first_chunk_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "chat first chunk role=%s session_id=%s first_chunk_ms=%.0f chunk_chars=%d",
                role,
                session_id,
                stats.first_chunk_ms,
                len(chunk),
            )
        if item.counted_as_output:
            stats.chunk_count += 1
            stats.output_chars += len(chunk)
        yield item
