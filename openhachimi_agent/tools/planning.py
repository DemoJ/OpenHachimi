"""任务规划与 TODO 追踪系统。"""

import logging
import json
from dataclasses import dataclass, field, asdict
from typing import Literal
from typing_extensions import TypedDict
from functools import wraps
import inspect
from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.core.deps import AgentDeps

logger = logging.getLogger(__name__)

class TodoTaskInput(TypedDict, total=False):
    id: int
    description: str
    parent_id: int
    depends_on: list[int]
    allowed_tools: list[str]
    success_criteria: str
    verification: str
    risk_level: Literal["low", "medium", "high"]

@dataclass
class TodoTask:
    id: int
    description: str
    status: Literal["pending", "in-progress", "done", "blocked"] = "pending"
    notes: str = ""
    parent_id: int | None = None
    depends_on: list[int] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    success_criteria: str = ""
    verification: str = ""
    risk_level: Literal["low", "medium", "high"] = "low"
    evidence: str = ""

@dataclass
class TodoState:
    goal: str = ""
    invariants: list[str] = field(default_factory=list)
    tasks: dict[int, TodoTask] = field(default_factory=dict)
    tool_calls_since_update: int = 0
    is_active: bool = False

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
            goal=str(data.get("goal", "")),
            invariants=[str(item) for item in data.get("invariants", []) if item],
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
    session_state = ctx.deps.session_state
    if "todo_state" not in session_state:
        session_state["todo_state"] = _load_state(ctx)
    return session_state["todo_state"]


def _validate_tasks(tasks: dict[int, TodoTask]) -> None:
    if not tasks:
        raise ModelRetry("TODO 任务列表不能为空")

    valid_ids = list(tasks.keys())
    for task_id, task in tasks.items():
        if task_id != task.id:
            raise ModelRetry(f"任务 ID 不一致：key={task_id}, id={task.id}")
        if not task.description.strip():
            raise ModelRetry(f"任务 {task_id} 的 description 不能为空")
        if task.parent_id == task_id:
            raise ModelRetry(f"任务 {task_id} 不能把自己设为父任务")
        for dep_id in task.depends_on:
            if dep_id == task_id:
                raise ModelRetry(f"任务 {task_id} 不能依赖自身")
            if dep_id not in tasks:
                raise ModelRetry(
                    f"任务 {task_id} 依赖不存在的任务 {dep_id}。"
                    f"当前所有有效任务 ID 为：{valid_ids}，请只在 depends_on 中填写这些 ID。"
                )

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(task_id: int) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            cycle_path = list(visiting) + [task_id]
            raise ModelRetry(f"TODO 依赖中存在循环依赖，涉及任务：{cycle_path}。请重新设计 depends_on，确保依赖关系是有向无环图。")
        visiting.add(task_id)
        for dep_id in tasks[task_id].depends_on:
            visit(dep_id)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def _all_dependencies_done(state: TodoState, task: TodoTask) -> bool:
    return all(state.tasks[dep_id].status == "done" for dep_id in task.depends_on)


def _refresh_active_flag(state: TodoState) -> None:
    if state.tasks and all(t.status == "done" for t in state.tasks.values()):
        state.is_active = False


def create_todos(
    ctx: RunContext[AgentDeps],
    tasks: list[TodoTaskInput | str],
    goal: str = "",
    invariants: list[str] | None = None,
) -> str:
    """
    创建一个全新的 TODO 任务列表，用于规划复杂的步骤。
    
    当你需要执行一个包含多个步骤的复杂任务时（如搜集多方面信息、写复杂代码、进行深度研究），
    请首先调用此工具将模糊意图拆解为具体的 TODO 列表。
    
    参数说明：
    - goal: 记录本计划要完成的用户目标。
    - invariants: 记录计划和执行过程不可违反的约束列表（例如 ["不可修改 API 签名", "必须保持向后兼容"]）。如果没有约束，必须省略此参数或传入空数组 []，绝不要传入字符串 'None' 或 'null'。
    - tasks: 任务列表。可以是简单的字符串列表（代表每个任务的描述），也可以是包含详细配置的字典列表（推荐用于复杂任务）。
      支持的字段包括 id, description, parent_id, depends_on, allowed_tools, success_criteria, verification, risk_level。
    
    调用后，请逐一执行工具完成任务，并使用 update_todo 及时更新状态。
    """
    if not tasks:
        raise ModelRetry("TODO 任务列表不能为空")

    def _coerce_int(val, field_name: str) -> int | None:
        """尝试将值强制转为 int，失败时返回 None 而非立即报错。"""
        if val is None:
            return None
        if isinstance(val, int):
            return val
        try:
            return int(val)
        except (TypeError, ValueError):
            logger.warning("create_todos: 无法将 %s=%r 转为整数，已忽略", field_name, val)
            return None

    new_tasks: dict[int, TodoTask] = {}
    next_id = 1
    # 预扫描最大 ID，兼容整数和字符串形式（如 LLM 返回 "1" 而非 1）
    for item in tasks:
        if isinstance(item, dict) and "id" in item:
            coerced = _coerce_int(item["id"], "id")
            if coerced is not None and coerced >= next_id:
                next_id = coerced + 1

    for item in tasks:
        if isinstance(item, str):
            t_id = next_id
            next_id += 1
            if t_id in new_tasks:
                raise ModelRetry(f"重复的任务 ID：{t_id}")
            new_tasks[t_id] = TodoTask(id=t_id, description=item)
        else:
            raw_id = item.get("id")
            t_id = _coerce_int(raw_id, "id")
            if t_id is None:
                # ID 无法转换则自动分配
                t_id = next_id
                next_id += 1
            if t_id in new_tasks:
                raise ModelRetry(f"重复的任务 ID：{t_id}，请为每个任务指定唯一 ID")

            desc = item.get("description", "Unnamed Task")
            raw_parent = item.get("parent_id")
            parent_id = _coerce_int(raw_parent, "parent_id")

            raw_depends = item.get("depends_on", [])
            if not isinstance(raw_depends, list):
                raw_depends = []
            # 同时兼容整数和字符串形式的依赖 ID
            depends_on = [c for dep in raw_depends if (c := _coerce_int(dep, "depends_on")) is not None]

            allowed_tools = item.get("allowed_tools", [])
            if not isinstance(allowed_tools, list):
                allowed_tools = []
            risk_level = item.get("risk_level", "low")
            if risk_level not in {"low", "medium", "high"}:
                risk_level = "low"
            
            new_tasks[t_id] = TodoTask(
                id=t_id,
                description=str(desc),
                parent_id=parent_id,
                depends_on=depends_on,
                allowed_tools=[str(tool) for tool in allowed_tools],
                success_criteria=str(item.get("success_criteria", "")),
                verification=str(item.get("verification", "")),
                risk_level=risk_level,
            )

    _validate_tasks(new_tasks)

    state = _get_state(ctx)
    task_frame = ctx.deps.session_state.get("task_frame", {})
    state.goal = goal or str(task_frame.get("goal", ""))
    inherited_invariants = task_frame.get("invariants", [])
    merged_invariants = list(invariants or [])
    if isinstance(inherited_invariants, list):
        merged_invariants.extend(str(item) for item in inherited_invariants if item)
    state.invariants = list(dict.fromkeys(merged_invariants))
    state.tasks = new_tasks
    state.tool_calls_since_update = 0
    state.is_active = True
    _save_state(ctx, state)
    logger.info("Created %d TODO tasks for session %s.", len(tasks), ctx.deps.session_id)
    return get_todos(ctx)


def update_todo(
    ctx: RunContext[AgentDeps],
    task_id: int,
    status: Literal["pending", "in-progress", "done", "blocked"],
    notes: str = "",
    evidence: str = "",
) -> str:
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
    if status in {"in-progress", "done"} and not _all_dependencies_done(state, task):
        missing = [dep_id for dep_id in task.depends_on if state.tasks[dep_id].status != "done"]
        return f"错误：任务 {task_id} 的依赖尚未完成：{missing}"
    if status == "done" and task.success_criteria and not (notes or evidence):
        return f"错误：任务 {task_id} 设置了成功标准，标记 done 时必须提供 notes 或 evidence。"
        
    task.status = status
    if notes:
        task.notes = notes
    if evidence:
        task.evidence = evidence
        
    # 重置调用计数器
    state.tool_calls_since_update = 0
    _refresh_active_flag(state)
    _save_state(ctx, state)
    logger.info("Updated TODO %d to %s for session %s", task_id, status, ctx.deps.session_id)
    
    return get_todos(ctx)


def get_todos(ctx: RunContext[AgentDeps]) -> str:
    """查看当前所有的 TODO 任务及其状态。"""
    state = _get_state(ctx)
    if not state.tasks:
        return "当前没有活动的 TODO 任务。"
        
    lines = ["## 当前 TODO 列表："]
    if state.goal:
        lines.append(f"目标：{state.goal}")
    if state.invariants:
        lines.append("不可变约束：")
        for invariant in state.invariants:
            lines.append(f"- {invariant}")
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
        criteria_str = f" [验收: {task.success_criteria}]" if task.success_criteria else ""
        evidence_str = f" [证据: {task.evidence}]" if task.evidence else ""
        lines.append(f"{indent}{box} {task.id}. {task.description}{deps_str}{criteria_str}{note_str}{evidence_str}")
        
        children = [t for t in state.tasks.values() if t.parent_id == task.id]
        for child in children:
            render_task(child, depth + 1)

    for task in state.tasks.values():
        if task.parent_id is None or task.parent_id not in valid_ids:
            render_task(task, 0)
        
    return "\n".join(lines)


def get_current_task_for_tool(ctx: RunContext[AgentDeps], tool_name: str) -> TodoTask | None:
    """检查当前是否有匹配的 in-progress 任务。

    采用软提醒策略：即使没有 in-progress 任务也允许执行，
    避免 LLM 并行调用 update_todo + 其他工具时因竞态条件而失败。
    """

    state = _get_state(ctx)
    if not state.is_active or not state.tasks:
        return None

    pending = [task for task in state.tasks.values() if task.status != "done"]
    if not pending:
        state.is_active = False
        _save_state(ctx, state)
        return None

    in_progress = [task for task in state.tasks.values() if task.status == "in-progress"]
    if len(in_progress) != 1:
        # 软提醒：不阻塞执行，仅记录日志
        logger.warning(
            "执行 %s 时没有恰好一个 in-progress TODO（当前 %d 个），建议使用 update_todo 更新状态。",
            tool_name, len(in_progress)
        )
        return None

    task = in_progress[0]
    if not _all_dependencies_done(state, task):
        logger.warning("任务 %d 的依赖尚未完成，但仍允许执行 %s。", task.id, tool_name)
        return task
    if task.allowed_tools and tool_name not in task.allowed_tools:
        allowed = ", ".join(task.allowed_tools)
        logger.warning("当前任务 %d 未授权工具 %s（允许：%s），但仍允许执行。", task.id, tool_name, allowed)
    return task


def with_execution_guard(func):
    """Block mutating tools when an active plan has no valid in-progress task."""

    tool_name = getattr(func, "__name__", "unknown_tool")
    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def async_wrapper(ctx, *args, **kwargs):
            get_current_task_for_tool(ctx, tool_name)
            return await func(ctx, *args, **kwargs)
        return async_wrapper

    @wraps(func)
    def sync_wrapper(ctx, *args, **kwargs):
        get_current_task_for_tool(ctx, tool_name)
        return func(ctx, *args, **kwargs)
    return sync_wrapper


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
