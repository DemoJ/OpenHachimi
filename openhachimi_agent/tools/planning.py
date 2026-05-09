"""任务规划与 TODO 追踪系统。"""

import logging
from dataclasses import dataclass, field
from typing import Literal
from functools import wraps
import inspect

from pydantic_ai import RunContext
from openhachimi_agent.core.config import AppConfig

logger = logging.getLogger(__name__)

@dataclass
class TodoTask:
    id: int
    description: str
    status: Literal["pending", "in-progress", "done"] = "pending"
    notes: str = ""

@dataclass
class TodoState:
    tasks: dict[int, TodoTask] = field(default_factory=dict)
    tool_calls_since_update: int = 0
    is_active: bool = False

# 因为这是一个本地 CLI 工具，生命周期单例状态是完全可以接受的
_GLOBAL_TODO_STATE = TodoState()


def create_todos(ctx: RunContext[AppConfig], tasks: list[str]) -> str:
    """
    创建一个全新的 TODO 任务列表，用于规划复杂的步骤。
    
    当你需要执行一个包含多个步骤的复杂任务时（如搜集多方面信息、写复杂代码、进行深度研究），
    请首先调用此工具将模糊意图拆解为具体的 TODO 列表。
    
    调用后，请逐一执行工具完成任务，并使用 update_todo 及时更新状态。
    """
    del ctx
    _GLOBAL_TODO_STATE.tasks.clear()
    _GLOBAL_TODO_STATE.tool_calls_since_update = 0
    _GLOBAL_TODO_STATE.is_active = True
    
    for i, desc in enumerate(tasks, start=1):
        _GLOBAL_TODO_STATE.tasks[i] = TodoTask(id=i, description=desc)
        
    logger.info("Created %d TODO tasks.", len(tasks))
    return get_todos(None)  # type: ignore


def update_todo(ctx: RunContext[AppConfig], task_id: int, status: Literal["pending", "in-progress", "done"], notes: str = "") -> str:
    """
    更新某个 TODO 任务的状态。
    当你开始一个任务或完成一个任务时，必须调用此工具更新状态。
    
    参数：
    - task_id: 任务的数字 ID
    - status: 必须是 "in-progress" 或 "done" 或 "pending"
    - notes: 简要记录该任务的进展或结论
    """
    del ctx
    task = _GLOBAL_TODO_STATE.tasks.get(task_id)
    if not task:
        return f"错误：未找到 ID 为 {task_id} 的任务。"
        
    task.status = status
    if notes:
        task.notes = notes
        
    # 重置调用计数器
    _GLOBAL_TODO_STATE.tool_calls_since_update = 0
    logger.info("Updated TODO %d to %s", task_id, status)
    
    return get_todos(None)  # type: ignore


def get_todos(ctx: RunContext[AppConfig]) -> str:
    """查看当前所有的 TODO 任务及其状态。"""
    del ctx
    if not _GLOBAL_TODO_STATE.tasks:
        return "当前没有活动的 TODO 任务。"
        
    lines = ["## 当前 TODO 列表："]
    for t_id, t in _GLOBAL_TODO_STATE.tasks.items():
        box = "[ ]"
        if t.status == "in-progress":
            box = "[-]"
        elif t.status == "done":
            box = "[x]"
            
        note_str = f" (备注: {t.notes})" if t.notes else ""
        lines.append(f"{box} {t_id}. {t.description}{note_str}")
        
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
            return _inject_reminder_if_needed(result)
        return async_wrapper
    else:
        @wraps(func)
        def sync_wrapper(ctx, *args, **kwargs):
            result = func(ctx, *args, **kwargs)
            return _inject_reminder_if_needed(result)
        return sync_wrapper

def _inject_reminder_if_needed(result: str | dict | list | None) -> str | dict | list | None:
    if not _GLOBAL_TODO_STATE.is_active or not _GLOBAL_TODO_STATE.tasks:
        return result
        
    has_pending = any(t.status != "done" for t in _GLOBAL_TODO_STATE.tasks.values())
    if not has_pending:
        return result
        
    _GLOBAL_TODO_STATE.tool_calls_since_update += 1
    if _GLOBAL_TODO_STATE.tool_calls_since_update >= 3:
        reminder = "\n\n[System Reminder: 你有正在进行中的 TODO 任务，如果已完成某些步骤，请务必使用 update_todo 更新状态！不要忘记整体的规划进度！]"
        _GLOBAL_TODO_STATE.tool_calls_since_update = 0
        if isinstance(result, str):
            return result + reminder
            
    return result
