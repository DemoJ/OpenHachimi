"""Streaming event bridge and queue consumption helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
from openhachimi_agent.core.redaction import REDACTED, redact_tool_args, summarize_redacted
from openhachimi_agent.service.agent_runtime.context import OperationState
from openhachimi_agent.transport.api_models import ArtifactRef


logger = logging.getLogger(__name__)
STREAM_DONE = object()

# 流式输出时的信号标签:在最终回复后追加的提示文案。
# 与命令机制无关,仅用于流式事件渲染。
SIGNAL_LABELS: tuple[tuple[str, str], ...] = (
    ("final_verification_signal", "[最终验证未通过] 当前执行结果仍缺少完成证据:"),
)


@dataclass
class StreamEventItem:
    type: Literal["text", "tool", "system", "artifact"]
    text: str
    tool_name: str | None = None
    tool_icon: str | None = None
    temporary: bool = False
    counted_as_output: bool = True
    artifact: ArtifactRef | None = None


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


def summarize_tool_args(args: object, max_chars: int = 160) -> str:
    return summarize_redacted(args, max_chars=max_chars)


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


def _task_description(item: object) -> tuple[str, str]:
    """返回 (description, success_criteria) 二元组。

    description 形如 ``1. 探测邮件发送能力``(若有 id);
    success_criteria 是验收文本(可空)。供 ``_tasks_block`` 渲染成两行/一行。
    """
    if isinstance(item, str):
        return _compact(item, 120), ""
    if isinstance(item, dict):
        desc = _compact(item.get("description", "Unnamed Task"), 120)
        task_id = item.get("id")
        prefix = f"{task_id}. " if task_id not in (None, "") else ""
        criteria = _compact(item.get("success_criteria", ""), 120)
        return f"{prefix}{desc}", criteria
    return _compact(item, 120), ""


def _tasks_block(tasks: object, max_items: int = 6) -> str:
    """把任务列表渲染为**多行**块,每条任务一行(有验收时再缩进一行)。

    用于 create_todos 工具卡片的明细区。TG / WebUI 在 conversation 模式下会把整
    个工具行原样展示——单行包含 ``\\n`` 时换行渲染,不需要前端额外协议支持。
    """
    if not isinstance(tasks, list):
        return _compact(tasks, 200)
    rendered: list[str] = []
    for item in tasks[:max_items]:
        desc, criteria = _task_description(item)
        rendered.append(f"  {desc}")
        if criteria:
            # 验收一行单独缩进,视觉上从属上一条
            rendered.append(f"     验收：{criteria}")
    if len(tasks) > max_items:
        rendered.append(f"  …等 {len(tasks) - max_items} 项")
    return "\n".join(rendered)


def _tool_detail(tool_name: str, args: dict[str, object]) -> str:
    if not args:
        return ""

    if tool_name == "create_todos":
        # 多行渲染:目标一行,计划列表每一项一行(有验收再缩进一行)。
        # 单行包含 ``\n`` 时 TG / WebUI 在 conversation 模式会自然换行,不需要
        # 前端额外协议。改前是单行 "；" 拼接,长 plan 在 telegram 上会糊成一坨。
        goal = _compact(args.get("goal", ""), 120)
        tasks = args.get("tasks", [])
        lines: list[str] = []
        if goal:
            lines.append(f"目标：{goal}")
        if isinstance(tasks, list) and tasks:
            lines.append(f"计划（共 {len(tasks)} 项）：")
            block = _tasks_block(tasks)
            if block:
                lines.append(block)
        return "\n".join(lines)

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
    if not detail:
        return f"{icon} {action}"
    # detail 含换行时(目前只有 create_todos),把它折成块状渲染:标题独占一行,
    # 明细块换行接下来,避免和 presenter 的 "• " bullet 拼成一坨长串。
    if "\n" in detail:
        return f"{icon} {action}：\n{detail}"
    return f"{icon} {action}：{detail}"


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
        # clarify_user 是 deferred 工具:它会抛 CallDeferred 让 run 在 graph 层
        # 立即终止,question 参数本身就是要发给用户看的完整自然语言追问 ——
        # turn.run_agent 的 deferred outcome 分支会把它当作本轮 assistant 回复
        # 完整输出。这里如果再 emit 一条标准"工具卡片"事件,UI 上就会看到一行
        # 截断的 ``🔧 clarify_user：{"question": "...获取的授权码（不是Q...``
        # 之后又紧跟一段完整的 question 文本,既丑又重复。所以静默吞掉这条事件。
        if tool_name == "clarify_user":
            return None
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
    text_buffer: list[str] | None = None,
) -> Callable[[object, object], Awaitable[None]]:
    async def handle_stream_events(_ctx: object, stream_event: object) -> None:
        async for event in stream_event:  # type: ignore[attr-defined]
            _update_operation_from_event(operation_state, event)
            if item := event_item_from_stream_event(event):
                if text_buffer is not None and item.type == "text":
                    text_buffer.append(item.text)
                    continue
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
    return StreamEventItem(type="system", text=text, counted_as_output=False)


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
