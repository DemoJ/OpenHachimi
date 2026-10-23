"""任务规划与 TODO 追踪系统。

计划是一份扁平、按顺序排列的清单:列表顺序即优先级,不再有依赖图。"同一时间只做一项"
只是建议,不做硬拦截——硬拦截会让模型花额外的工具调用去先改别的项的状态。
"""

import json
import logging

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.core.deps import AgentDeps

# 数据模型、状态取值、会话持久化在 planning_state;工具结果后的提醒在 planning_reminder。
# 这里重新导出,旧的 ``from openhachimi_agent.tools.planning import ...`` 写法继续可用。
from openhachimi_agent.tools.planning_reminder import (  # noqa: F401
    TODO_REMINDER,
    _inject_reminder_if_needed,
    with_todo_reminder,
)
from openhachimi_agent.tools.planning_state import (  # noqa: F401
    OPEN_STATUSES,
    TodoState,
    TodoTask,
    TodoTaskInput,
    _get_state,
    _refresh_active_flag,
    _save_state,
    has_open_tasks,
    normalize_status,
)

logger = logging.getLogger(__name__)


def _validate_tasks(tasks: dict[int, TodoTask]) -> None:
    if not tasks:
        raise ModelRetry("TODO 任务列表不能为空")
    for task_id, task in tasks.items():
        if task_id != task.id:
            raise ModelRetry(f"任务 ID 不一致：key={task_id}, id={task.id}")
        if not task.description.strip():
            raise ModelRetry(f"任务 {task_id} 的 description 不能为空")


_STATUS_BOXES = {"in-progress": "[-]", "done": "[x]", "blocked": "[!]"}


def _format_status_box(status: str) -> str:
    """Return display box marker for a task status."""
    return _STATUS_BOXES.get(status, "[ ]")


def _render_task_lines(state: TodoState) -> str:
    """把任务渲染成扁平列表,顺序即优先级。不含标题行,由调用方加。"""
    lines: list[str] = []
    for task in state.tasks.values():
        box = _format_status_box(task.status)
        note_str = f" (备注: {task.notes})" if task.notes else ""
        evidence_str = f" [证据: {task.evidence}]" if task.evidence else ""
        lines.append(f"{box} {task.id}. {task.description}{note_str}{evidence_str}")
    return "\n".join(lines)


def _render_plan(state: TodoState) -> list[str]:
    """目标 + 不可变约束 + 任务列表。"""
    lines: list[str] = []
    if state.goal:
        lines.append(f"目标：{state.goal}")
    if state.invariants:
        lines.append("不可变约束：")
        lines.extend(f"- {invariant}" for invariant in state.invariants)
    lines.append(_render_task_lines(state))
    return lines


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


def _coerce_int(val: object, field_name: str) -> int | None:
    """尝试将值强制转为 int(兼容 LLM 输出的 "1"),失败时返回 None 而非报错。"""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("planning: 无法将 %s=%r 转为整数，已忽略", field_name, val)
        return None


def _parse_task_inputs(tasks: list[TodoTaskInput | str]) -> dict[int, TodoTask]:
    """把模型给的任务列表(字符串或 {id, description} 字典)转成按 id 索引的任务。"""
    explicit_ids = [
        coerced for item in tasks
        if isinstance(item, dict) and (coerced := _coerce_int(item.get("id"), "id")) is not None
    ]
    next_id = max([0, *explicit_ids]) + 1
    parsed: dict[int, TodoTask] = {}
    for item in tasks:
        if isinstance(item, str):
            task_id, description = None, item
        else:
            task_id, description = _coerce_int(item.get("id"), "id"), str(item.get("description", "Unnamed Task"))
        if task_id is None:
            task_id = next_id
            next_id += 1
        if task_id in parsed:
            raise ModelRetry(f"重复的任务 ID：{task_id}，请为每个任务指定唯一 ID")
        parsed[task_id] = TodoTask(id=task_id, description=description)
    return parsed


def _merge_tasks(existing: dict[int, TodoTask], incoming: dict[int, TodoTask]) -> dict[int, TodoTask]:
    """按 id 合并(参考 Hermes todo merge 语义):已有 id 只更新描述并保留 status/notes/evidence,
    新 id 追加,没出现在新列表里的旧 id 保留。"""
    merged = dict(existing)
    for task_id, task in incoming.items():
        if task_id in merged:
            merged[task_id].description = task.description
        else:
            merged[task_id] = task
    return merged


def _reject_overwrite_of_active_plan(state: TodoState) -> None:
    """计划还有 pending / in-progress 项时,不带 merge 的 create_todos 不能覆盖它。"""
    raise ModelRetry(
        f"已存在活动计划（{len(state.tasks)} 个任务，goal={state.goal!r}）。三选一：\n"
        "(a) 同一目标的细化：用 update_todo 修改具体任务，不要重新 create_todos；\n"
        "(b) 修订计划：调 create_todos(merge=True, tasks=[...])，"
        "按 id 合并而非覆盖（保留旧任务的 status/evidence/notes）；\n"
        "(c) 全新无关任务：先把旧计划所有 pending/in-progress 任务 "
        "update_todo(..., status=\"blocked\")，再 create_todos(merge=False, ...) 覆盖。"
    )


def create_todos(
    ctx: RunContext[AgentDeps],
    tasks: list[TodoTaskInput | str],
    goal: str = "",
    invariants: list[str] | str | None = None,
    merge: bool = False,
) -> str:
    """
    管理本会话的任务清单,用于把复杂任务拆成可追踪步骤、跟进进度。

    **何时用**:3 个及以上步骤、或多工具串联的复杂任务。1-2 步的简单任务直接做,
    不要建清单。列表顺序即执行顺序。建议同一时间只让一个任务 in-progress。
    某项走不通就 update_todo 标 blocked,并用 merge=True 追加替代步骤——不要硬走
    原计划,也不要中途无 merge 地全量覆盖丢失进度。

    参数说明:
    - goal: 记录本计划要完成的用户目标。
    - invariants: 计划和执行过程不可违反的约束列表(例如 ["不可修改 API 签名", "必须保持向后兼容"])。
      没有约束时省略或传 []。
    - tasks: 任务列表。可以是简单字符串列表(每个元素代表 description),也可以是字典列表。
      字典可选字段:id, description。
    - merge: 默认 False,即"全量替换"语义。当已存在一个活动计划时,仅传 merge=True
      才按 id 合并新旧任务(保留旧任务的 status/evidence/notes,新增 id 追加,未列出的旧 id 保留);
      不传 merge=True 的 create_todos 会拒绝覆盖既有活动计划,避免静默丢失进度。

    调用后,请逐一执行工具完成任务,并使用 update_todo 及时更新状态。
    """
    if not tasks:
        raise ModelRetry("TODO 任务列表不能为空")
    new_tasks = _parse_task_inputs(tasks)
    state = _get_state(ctx)
    if state.is_active and has_open_tasks(state.tasks) and not merge:
        _reject_overwrite_of_active_plan(state)
    merging = merge and bool(state.tasks)
    if merging:
        new_tasks = _merge_tasks(state.tasks, new_tasks)
    _validate_tasks(new_tasks)

    state.goal = goal or state.goal
    merged_invariants = _normalize_invariants(invariants)
    if merge and state.invariants:
        merged_invariants = list(state.invariants) + merged_invariants
    state.invariants = list(dict.fromkeys(merged_invariants))
    state.tasks = new_tasks
    state.tool_calls_since_update = 0
    state.is_active = True
    _save_state(ctx, state)
    logger.info("%s %d TODO tasks for session %s.", "Merged" if merging else "Created", len(tasks), ctx.deps.session_id)
    # 返回完整列表:模型要靠状态标记、描述、备注推理当前进度
    return f"已创建 {len(state.tasks)} 个 TODO 任务。\n" + "\n".join(_render_plan(state))


def _other_in_progress_hint(state: TodoState, task_id: int) -> str:
    """开始新任务时还有别的项在进行中:不拦截,只提醒一句。"""
    others = [other.id for other in state.tasks.values() if other.id != task_id and other.status == "in-progress"]
    if not others:
        return ""
    return f" 提示：任务 {others} 仍标记为进行中，做完后记得更新它们的状态。"


def _update_summary(state: TodoState, task_id: int, status: str) -> str:
    remaining = [task for task in state.tasks.values() if task.status in OPEN_STATUSES]
    summary = f"已更新任务 {task_id} → {status}。"
    summary += f" 剩余 {len(remaining)} 项待办。" if remaining else " 所有 TODO 已完成！"
    return summary


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
    # 参数格式不对时只返回错误字符串,不抛异常:抛了会计入 pydantic-ai 的 retries,
    # 累计几次就 UnexpectedModelBehavior,整轮报废(GLM/Qwen 常把 task_id 写成 "1")。
    parsed_id = _coerce_int(task_id.strip() if isinstance(task_id, str) else task_id, "task_id")
    if parsed_id is None:
        return f"错误：task_id 必须是数字,但收到 {task_id!r}。请检查 TODO 列表 ID 后重试。"
    normalized = normalize_status(status)
    if normalized is None:
        return f"错误：未识别的 status {status!r}。请使用以下之一:pending / in-progress / done / blocked。"

    state = _get_state(ctx)
    task = state.tasks.get(parsed_id)
    if not task:
        return f"错误：未找到 ID 为 {parsed_id} 的任务。"
    hint = _other_in_progress_hint(state, parsed_id) if normalized == "in-progress" else ""
    task.status = normalized  # type: ignore[assignment]
    if notes:
        task.notes = notes
    if evidence:
        task.evidence = evidence
    state.tool_calls_since_update = 0
    # 把 blocked 项重新打开(例如用户补齐了缺的信息)时,计划要重新激活。
    if normalized in OPEN_STATUSES:
        state.is_active = True
    _refresh_active_flag(state)
    _save_state(ctx, state)
    logger.info("Updated TODO %d to %s for session %s", parsed_id, normalized, ctx.deps.session_id)
    return f"{_update_summary(state, parsed_id, normalized)}{hint}\n\n{_render_task_lines(state)}"


def get_todos(ctx: RunContext[AgentDeps]) -> str:
    """查看当前所有的 TODO 任务及其状态。"""
    state = _get_state(ctx)
    if not state.tasks:
        return "当前没有活动的 TODO 任务。"
    return "\n".join(["## 当前 TODO 列表：", *_render_plan(state)])
