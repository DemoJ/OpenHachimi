"""任务规划与 TODO 追踪系统。"""

import json
import logging
from dataclasses import dataclass, field
from typing import Literal
from typing_extensions import TypedDict
from functools import wraps
import inspect

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.core.deps import AgentDeps

logger = logging.getLogger(__name__)


class TodoTaskInput(TypedDict, total=False):
    id: int
    description: str
    depends_on: list[int]

@dataclass
class TodoTask:
    id: int
    description: str
    status: Literal["pending", "in-progress", "done", "blocked"] = "pending"
    notes: str = ""
    depends_on: list[int] = field(default_factory=list)
    evidence: str = ""

@dataclass
class TodoState:
    goal: str = ""
    invariants: list[str] = field(default_factory=list)
    tasks: dict[int, TodoTask] = field(default_factory=dict)
    tool_calls_since_update: int = 0
    is_active: bool = False

def _load_state(ctx: RunContext[AgentDeps]) -> TodoState:
    """从 SessionStore 加载本会话 TODO 状态。

    旧实现读 ``{memory_dir}/todos/{session_id}.json``,现在改走
    ``ctx.deps.session_store.load_todo_state``。Store 内部已经把"文件不存在 /
    JSON 解析失败 / 字段类型异常"统一兜底为空 ``TodoState()`` + log warning,
    与原本 ``_load_state`` 的健壮性语义一致。
    """
    store = getattr(ctx.deps, "session_store", None)
    if store is None:
        # 离线/构造期 deps 无 session_store:返回空,与旧路径"文件不存在"等价
        return TodoState()
    return store.load_todo_state(ctx.deps.session_id)

def _save_state(ctx: RunContext[AgentDeps], state: TodoState):
    """把当前 TODO 状态写回 SessionStore。"""
    store = getattr(ctx.deps, "session_store", None)
    if store is None:
        logger.warning("session_store missing on deps, TODO state will not be persisted")
        return
    try:
        store.save_todo_state(ctx.deps.session_id, state)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to save TODO state session_id=%s: %s", ctx.deps.session_id, e)

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


def _format_status_box(status: str) -> str:
    """Return display box marker for a task status."""
    if status == "in-progress":
        return "[-]"
    if status == "done":
        return "[x]"
    return "[ ]"


def _render_task_lines(state: TodoState) -> str:
    """Render all tasks into formatted lines as a flat priority-ordered list.

    Excludes the \"## 当前 TODO 列表：\" header — callers may want
    different prefixes (create summary, update summary, etc.).

    列表顺序即优先级(对齐 Hermes todo schema 语义),不再用 parent_id 嵌套表达
    层级——嵌套让模型分心构造图结构,扁平顺序更省力也更易读。
    """
    lines: list[str] = []

    for task in state.tasks.values():
        box = _format_status_box(task.status)
        deps_str = f" [依赖: {', '.join(map(str, task.depends_on))}]" if task.depends_on else ""
        note_str = f" (备注: {task.notes})" if task.notes else ""
        evidence_str = f" [证据: {task.evidence}]" if task.evidence else ""
        lines.append(f"{box} {task.id}. {task.description}{deps_str}{note_str}{evidence_str}")

    return "\n".join(lines)


def _refresh_active_flag(state: TodoState) -> None:
    if state.tasks and all(t.status == "done" for t in state.tasks.values()):
        state.is_active = False


def _normalize_invariants(invariants: list[str] | str | None) -> list[str]:
    if invariants is None:
        return []
    if isinstance(invariants, list):
        return [str(item) for item in invariants if item]
    text = invariants.strip()
    if not text or text.lower() in {"none", "null"}:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [text]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    if parsed is None:
        return []
    return [str(parsed)]


def create_todos(
    ctx: RunContext[AgentDeps],
    tasks: list[TodoTaskInput | str],
    goal: str = "",
    invariants: list[str] | str | None = None,
    merge: bool = False,
) -> str:
    """
    管理本会话的任务清单,用于把复杂任务拆成可追踪步骤、跟进进度。

    **何时用**:3 个及以上步骤、或多文件、或多工具串联的复杂任务。1-2 步的简单
    任务直接做,不要建清单。列表顺序即优先级。**同一时间只让一个任务 in-progress**。
    某项走不通就 update_todo 标 blocked,并用 merge=True 追加替代步骤——不要硬走
    原计划,也不要中途无 merge 地全量覆盖丢失进度。

    参数说明:
    - goal: 记录本计划要完成的用户目标。
    - invariants: 计划和执行过程不可违反的约束列表(例如 ["不可修改 API 签名", "必须保持向后兼容"])。
      没有约束时省略或传 []。
    - tasks: 任务列表。可以是简单字符串列表(每个元素代表 description),也可以是字典列表。
      字典可选字段:id, description, depends_on。
    - merge: 默认 False,即"全量替换"语义。当已存在一个活动计划时,仅传 merge=True
      才按 id 合并新旧任务(保留旧任务的 status/evidence/notes,新增 id 追加,未列出的旧 id 保留);
      不传 merge=True 的 create_todos 会拒绝覆盖既有活动计划,避免静默丢失进度。

    调用后,请逐一执行工具完成任务,并使用 update_todo 及时更新状态。
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

            raw_depends = item.get("depends_on", [])
            if not isinstance(raw_depends, list):
                raw_depends = []
            # 同时兼容整数和字符串形式的依赖 ID
            depends_on = [c for dep in raw_depends if (c := _coerce_int(dep, "depends_on")) is not None]

            new_tasks[t_id] = TodoTask(
                id=t_id,
                description=str(desc),
                depends_on=depends_on,
            )

    state = _get_state(ctx)

    # 跨轮再次 create_todos 且未传 merge=True 时拒绝,避免静默覆盖既有活动计划。
    # 同轮内(主 agent ReAct 循环里模型主动重写计划)不拦截,允许全量替换 —— 主 agent
    # 拿到新工具证据后调整计划是正常行为,守卫只会把它逼进死循环。
    if state.is_active and state.tasks and not merge:
        raise ModelRetry(
            f"已存在活动计划（{len(state.tasks)} 个任务，"
            f"goal={state.goal!r}）。三选一：\n"
            f"(a) 同一目标的细化：用 update_todo 修改具体任务，不要重新 create_todos；\n"
            f"(b) 修订计划：调 create_todos(merge=True, tasks=[...])，"
            f"按 id 合并而非覆盖（保留旧任务的 status/evidence/notes）；\n"
            f"(c) 全新无关任务：先把旧计划所有 pending/in-progress 任务 "
            f"update_todo(..., status=\"blocked\")，再 create_todos(merge=False, ...) 覆盖。"
        )
    if merge and state.tasks:
        # merge 语义参考 Hermes tools/todo_tool.py:
        # - 已有 id:用新值更新 description/depends_on,保留 status/evidence/notes/
        #   tool_calls_since_update;
        # - 新增 id:追加;
        # - 未在新列表的旧 id:保留。
        merged: dict[int, TodoTask] = dict(state.tasks)
        for new_id, new_task in new_tasks.items():
            existing = merged.get(new_id)
            if existing is None:
                merged[new_id] = new_task
                continue
            existing.description = new_task.description
            existing.depends_on = new_task.depends_on
        new_tasks = merged

    _validate_tasks(new_tasks)

    task_frame = ctx.deps.session_state.get("task_frame", {})
    state.goal = goal or state.goal or str(task_frame.get("goal", ""))
    inherited_invariants = task_frame.get("invariants", [])
    merged_invariants = _normalize_invariants(invariants)
    if isinstance(inherited_invariants, list):
        merged_invariants.extend(str(item) for item in inherited_invariants if item)
    if merge and state.invariants:
        merged_invariants = list(state.invariants) + merged_invariants
    state.invariants = list(dict.fromkeys(merged_invariants))
    state.tasks = new_tasks
    state.tool_calls_since_update = 0
    state.is_active = True
    _save_state(ctx, state)
    logger.info(
        "%s %d TODO tasks for session %s.",
        "Merged" if merge and state.tasks else "Created",
        len(tasks),
        ctx.deps.session_id,
    )
    # 返回格式化后的 TODO 列表。虽然可能比纯摘要多占 tokens,
    # 但 LLM 调用方依赖这些格式(状态标记、描述、备注)来推理,
    # 纯摘要会导致断言失败。各轮次之间由工具调用 history 中的
    # 完整输出 snapshot 保证模型仍有上下文。
    lines: list[str] = []
    if state.goal:
        lines.append(f"目标：{state.goal}")
    if state.invariants:
        lines.append("不可变约束：")
        for inv in state.invariants:
            lines.append(f"- {inv}")
    lines.append(_render_task_lines(state))
    return f"已创建 {len(state.tasks)} 个 TODO 任务。\n" + "\n".join(lines)


def update_todo(
    ctx: RunContext[AgentDeps],
    task_id: int | str,
    status: str,
    notes: str = "",
    evidence: str = "",
) -> str:
    """
    更新某个 TODO 任务的状态、备注或证据。
    当你开始一个任务或完成一个任务时，必须调用此工具更新状态。

    参数：
    - task_id: 任务的数字 ID（也接受 "1" 这样的字符串形式，会自动转 int）
    - status: 必须是 "in-progress" / "done" / "pending" / "blocked"。
      也接受 "in_progress" / "in progress" / "inprogress" 这些常见变体，
      以及 "completed" / "finished" 等同义词，会自动归一化。
    - notes: 简要记录该任务的进展或结论
    - evidence: 标记 done 时可附上验证证据(如测试命令、产物路径、引用片段),便于回溯
    """
    # task_id 接受字符串形式(GLM/Qwen 等开源模型常生成 "1" 而非 1)。
    # 这里不抛 ValidationError——一旦抛了,pydantic_ai 会把它计入 max_retries,
    # 累计 3 次就 UnexpectedModelBehavior,把整轮报废。
    if isinstance(task_id, str):
        try:
            task_id = int(task_id.strip())
        except (TypeError, ValueError):
            return f"错误：task_id 必须是数字,但收到 {task_id!r}。请检查 TODO 列表 ID 后重试。"

    # status 归一化:模型常写 "in_progress"/"completed"/"finished"/"cancelled" 等。
    # 用别名表统一映射到 schema 接受的 4 个值,避免每次 LLM 拼写差异都触发 retry。
    _STATUS_ALIASES = {
        "pending": "pending",
        "todo": "pending",
        "open": "pending",
        "waiting": "pending",
        "in-progress": "in-progress",
        "in_progress": "in-progress",
        "in progress": "in-progress",
        "inprogress": "in-progress",
        "running": "in-progress",
        "doing": "in-progress",
        "wip": "in-progress",
        "started": "in-progress",
        "done": "done",
        "completed": "done",
        "complete": "done",
        "finished": "done",
        "success": "done",
        "ok": "done",
        "resolved": "done",
        "closed": "done",
        "blocked": "blocked",
        "block": "blocked",
        "stuck": "blocked",
        "failed": "blocked",
        "fail": "blocked",
        "cancelled": "blocked",
        "canceled": "blocked",
        "skipped": "blocked",
        "abandoned": "blocked",
    }
    normalized = _STATUS_ALIASES.get(str(status).strip().lower().replace("　", " "))
    if normalized is None:
        return (
            f"错误：未识别的 status {status!r}。"
            "请使用以下之一:pending / in-progress / done / blocked。"
        )
    status = normalized  # type: ignore[assignment]

    state = _get_state(ctx)
    task = state.tasks.get(task_id)
    if not task:
        return f"错误：未找到 ID 为 {task_id} 的任务。"
    if status == "in-progress":
        other_in_progress = [
            other for other in state.tasks.values()
            if other.id != task_id and other.status == "in-progress"
        ]
        if other_in_progress:
            active_ids = [other.id for other in other_in_progress]
            return (
                f"错误：已有任务正在进行：{active_ids}。"
                "请先将这些任务标记为 done、pending 或 blocked，再开始新的任务。"
            )
    if status in {"in-progress", "done"} and not _all_dependencies_done(state, task):
        missing = [dep_id for dep_id in task.depends_on if state.tasks[dep_id].status != "done"]
        return f"错误：任务 {task_id} 的依赖尚未完成：{missing}"

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

    # 标记本轮已查看过计划：让 output_validator 知道模型已经有了 TODO 上下文，
    # 不再强制 get_todos。注意直接用 get_todos 的完整输出会占用大量 tokens
    # 并且重置模型对"当前进行到哪里"的感知，改为简洁确认。
    session_state = ctx.deps.session_state
    if isinstance(session_state, dict):
        session_state["_plan_viewed_this_turn"] = True

    remaining = [t for t in state.tasks.values() if t.status not in {"done", "blocked"}]
    summary = f"已更新任务 {task_id} → {status}。"
    if remaining:
        summary += f" 剩余 {len(remaining)} 项待办。"
    else:
        summary += " 所有 TODO 已完成！"
    task_list = _render_task_lines(state)
    return f"{summary}\n\n{task_list}"


def get_todos(ctx: RunContext[AgentDeps]) -> str:
    """查看当前所有的 TODO 任务及其状态。"""
    state = _get_state(ctx)
    if not state.tasks:
        return "当前没有活动的 TODO 任务。"

    # 标记本轮已查看过计划，供 output_validator 判断是否需要强制 get_todos。
    session_state = ctx.deps.session_state
    if isinstance(session_state, dict):
        session_state["_plan_viewed_this_turn"] = True
        
    lines = ["## 当前 TODO 列表："]
    if state.goal:
        lines.append(f"目标：{state.goal}")
    if state.invariants:
        lines.append("不可变约束：")
        for invariant in state.invariants:
            lines.append(f"- {invariant}")
    lines.append(_render_task_lines(state))

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

    return result
