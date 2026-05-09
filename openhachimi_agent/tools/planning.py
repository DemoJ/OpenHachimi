"""任务规划与 TODO 追踪系统。"""

import logging
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Literal
from functools import wraps
import inspect
from pathlib import Path

from pydantic_ai import RunContext
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps

logger = logging.getLogger(__name__)

@dataclass
class TodoTask:
    id: int
    description: str
    status: Literal["pending", "in-progress", "done"] = "pending"
    notes: str = ""
    parent_id: int | None = None
    depends_on: list[int] = field(default_factory=list)

@dataclass
class TodoState:
    tasks: dict[int, TodoTask] = field(default_factory=dict)
    tool_calls_since_update: int = 0
    is_active: bool = False

_SESSION_TODO_STATES: dict[str, TodoState] = {}

def _get_todos_file_path(ctx: RunContext[AgentDeps]) -> Path:
    todos_dir = ctx.deps.config.memory_dir / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)
    return todos_dir / f"{ctx.deps.session_id}.json"

def _load_state(ctx: RunContext[AgentDeps]) -> TodoState:
    path = _get_todos_file_path(ctx)
    if not path.exists():
        return TodoState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = TodoState(
            tool_calls_since_update=data.get("tool_calls_since_update", 0),
            is_active=data.get("is_active", False)
        )
        for k, v in data.get("tasks", {}).items():
            state.tasks[int(k)] = TodoTask(**v)
        return state
    except Exception as e:
        logger.warning("Failed to load TODO state from %s: %s", path, e)
        return TodoState()

def _save_state(ctx: RunContext[AgentDeps], state: TodoState):
    path = _get_todos_file_path(ctx)
    try:
        data = asdict(state)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save TODO state to %s: %s", path, e)

def _get_state(ctx: RunContext[AgentDeps]) -> TodoState:
    session_id = ctx.deps.session_id
    if session_id not in _SESSION_TODO_STATES:
        if len(_SESSION_TODO_STATES) > 1000:
            _SESSION_TODO_STATES.clear() # simple eviction
        _SESSION_TODO_STATES[session_id] = _load_state(ctx)
    return _SESSION_TODO_STATES[session_id]


def create_todos(ctx: RunContext[AgentDeps], tasks: list[dict | str]) -> str:
    """
    创建一个全新的 TODO 任务列表，用于规划复杂的步骤。
    
    当你需要执行一个包含多个步骤的复杂任务时（如搜集多方面信息、写复杂代码、进行深度研究），
    请首先调用此工具将模糊意图拆解为具体的 TODO 列表。
    
    tasks 参数可以是一个简单的字符串列表（代表每个任务的描述），
    也可以是包含详细配置的字典列表（推荐用于复杂任务），支持如下字段：
    - id (int, 可选): 指定任务 ID（方便在依赖中引用）。如果未提供，将自动分配。
    - description (str): 任务描述。
    - parent_id (int, 可选): 指定父任务的 ID，用于构建层级/子任务结构。
    - depends_on (list[int], 可选): 依赖的任务 ID 列表，表示这些任务完成后才能开始当前任务。
    
    调用后，请逐一执行工具完成任务，并使用 update_todo 及时更新状态。
    """
    state = _get_state(ctx)
    state.tasks.clear()
    state.tool_calls_since_update = 0
    state.is_active = True
    
    next_id = 1
    for item in tasks:
        if isinstance(item, dict) and "id" in item:
            if isinstance(item["id"], int) and item["id"] >= next_id:
                next_id = item["id"] + 1

    for item in tasks:
        if isinstance(item, str):
            t_id = next_id
            next_id += 1
            state.tasks[t_id] = TodoTask(id=t_id, description=item)
        else:
            t_id = item.get("id")
            if t_id is None:
                t_id = next_id
                next_id += 1
            
            desc = item.get("description", "Unnamed Task")
            parent_id = item.get("parent_id")
            depends_on = item.get("depends_on", [])
            if not isinstance(depends_on, list):
                depends_on = []
            
            state.tasks[t_id] = TodoTask(
                id=t_id,
                description=desc,
                parent_id=parent_id,
                depends_on=depends_on
            )
        
    _save_state(ctx, state)
    logger.info("Created %d TODO tasks for session %s.", len(tasks), ctx.deps.session_id)
    return get_todos(ctx)


def update_todo(ctx: RunContext[AgentDeps], task_id: int, status: Literal["pending", "in-progress", "done"], notes: str = "") -> str:
    """
    更新某个 TODO 任务的状态。
    当你开始一个任务或完成一个任务时，必须调用此工具更新状态。
    
    参数：
    - task_id: 任务的数字 ID
    - status: 必须是 "in-progress" 或 "done" 或 "pending"
    - notes: 简要记录该任务的进展或结论
    """
    state = _get_state(ctx)
    task = state.tasks.get(task_id)
    if not task:
        return f"错误：未找到 ID 为 {task_id} 的任务。"
        
    task.status = status
    if notes:
        task.notes = notes
        
    # 重置调用计数器
    state.tool_calls_since_update = 0
    _save_state(ctx, state)
    logger.info("Updated TODO %d to %s for session %s", task_id, status, ctx.deps.session_id)
    
    return get_todos(ctx)


def get_todos(ctx: RunContext[AgentDeps]) -> str:
    """查看当前所有的 TODO 任务及其状态。"""
    state = _get_state(ctx)
    if not state.tasks:
        return "当前没有活动的 TODO 任务。"
        
    lines = ["## 当前 TODO 列表："]
    valid_ids = set(state.tasks.keys())
    
    def render_task(task: TodoTask, depth: int):
        indent = "  " * depth
        box = "[ ]"
        if task.status == "in-progress":
            box = "[-]"
        elif task.status == "done":
            box = "[x]"
            
        deps_str = f" [依赖: {', '.join(map(str, task.depends_on))}]" if task.depends_on else ""
        note_str = f" (备注: {task.notes})" if task.notes else ""
        lines.append(f"{indent}{box} {task.id}. {task.description}{deps_str}{note_str}")
        
        children = [t for t in state.tasks.values() if t.parent_id == task.id]
        for child in children:
            render_task(child, depth + 1)

    for task in state.tasks.values():
        if task.parent_id is None or task.parent_id not in valid_ids:
            render_task(task, 0)
        
    return "\n".join(lines)


def with_todo_reminder(func):
    """
    工具装饰器：如果开启了 TODO 规划，且超过 3 次调用未更新 TODO，
    则在工具返回结果中注入提醒。
    """
    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def async_wrapper(ctx, *args, **kwargs):
            result = await func(ctx, *args, **kwargs)
            return _inject_reminder_if_needed(ctx, result)
        return async_wrapper
    else:
        @wraps(func)
        def sync_wrapper(ctx, *args, **kwargs):
            result = func(ctx, *args, **kwargs)
            return _inject_reminder_if_needed(ctx, result)
        return sync_wrapper

def _inject_reminder_if_needed(ctx: RunContext[AgentDeps], result: str | dict | list | None) -> str | dict | list | None:
    state = _get_state(ctx)
    if not state.is_active or not state.tasks:
        return result
        
    has_pending = any(t.status != "done" for t in state.tasks.values())
    if not has_pending:
        return result
        
    state.tool_calls_since_update += 1
    if state.tool_calls_since_update >= 3:
        reminder = "\n\n[System Reminder: 你有正在进行中的 TODO 任务，如果已完成某些步骤，请务必使用 update_todo 更新状态！不要忘记整体的规划进度！]"
        state.tool_calls_since_update = 0
        _save_state(ctx, state)
        if isinstance(result, str):
            return result + reminder
        elif isinstance(result, dict):
            new_result = dict(result)
            if "output" in new_result and isinstance(new_result["output"], str):
                new_result["output"] += reminder
            elif "message" in new_result and isinstance(new_result["message"], str):
                new_result["message"] += reminder
            else:
                new_result["_todo_reminder"] = reminder.strip()
            return new_result
            
    _save_state(ctx, state)
    return result
