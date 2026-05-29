"""定时任务工具。"""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.scheduler.security import ensure_scheduler_action_allowed, ensure_scheduler_mutation_allowed
from openhachimi_agent.scheduler.service import ScheduledTaskService, delayed_schedule_expr, run_to_dict, task_to_dict
from openhachimi_agent.scheduler.store import ScheduledTaskStore


_READ_ACTIONS = {"list", "get", "list_runs", "read_inbox", "preview_delivery"}
_MUTATION_ACTIONS = {"create", "update", "update_delivery", "pause", "resume", "remove", "run", "mark_read"}


def _get_service(ctx: RunContext[AgentDeps]) -> ScheduledTaskService:
    db_path = ctx.deps.config.scheduler.db_path
    if not ctx.deps.config.scheduler.enabled or db_path is None:
        raise RuntimeError("定时任务未启用。")
    return ScheduledTaskService(ScheduledTaskStore(db_path))


def _origin_from_context(ctx: RunContext[AgentDeps], origin: dict[str, Any] | None = None) -> dict[str, Any]:
    if origin:
        return dict(origin)
    context = dict(ctx.deps.channel_context or {})
    if context:
        context.setdefault("session_id", ctx.deps.session_id)
        return context
    return {
        "type": "agent",
        "platform": "local",
        "session_id": ctx.deps.session_id,
        "created_via": "agent_tool",
    }


def _default_delivery_fallback(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is not None:
        return value
    return {
        "enabled": True,
        "mode": "inbox",
        "targets": [{"type": "inbox", "box": "default"}],
        "on": ["resolve_failed", "send_failed"],
    }


def _require_task_id(action: str, task_id: str | None) -> str:
    if not task_id:
        raise ValueError(f"action={action} 需要 task_id。修改、暂停、删除或运行任务前，请先 list/get 确认任务 ID，不要猜 ID。")
    return task_id


def _create_task(
    ctx: RunContext[AgentDeps],
    *,
    name: str,
    prompt: str,
    schedule_type: str,
    schedule_expr: str,
    timezone: str = "UTC",
    role: str | None = None,
    session_id: str | None = None,
    timeout_seconds: int | None = None,
    delivery_mode: str | None = None,
    delivery_targets: list[dict[str, Any]] | None = None,
    delivery_fallback: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
) -> dict[str, object]:
    ensure_scheduler_mutation_allowed(ctx.deps.run_mode)
    service = _get_service(ctx)
    task = service.create(
        name=name,
        prompt=prompt,
        schedule_type=schedule_type,
        schedule_expr=schedule_expr,
        timezone=timezone,
        role=role,
        session_id=session_id or ctx.deps.session_id,
        timeout_seconds=timeout_seconds,
        origin=_origin_from_context(ctx),
        delivery_mode=delivery_mode or ctx.deps.config.scheduler.delivery.default_mode,
        delivery_targets=delivery_targets,
        delivery_fallback=_default_delivery_fallback(delivery_fallback),
        execution_policy=execution_policy,
    )
    return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”已创建。"}


def list_scheduled_tasks(ctx: RunContext[AgentDeps], include_deleted: bool = False) -> dict[str, object]:
    """列出定时任务。纯查询工具，不会创建、修改、删除或触发任务。"""
    tasks = _get_service(ctx).list(include_deleted=include_deleted)
    return {"success": True, "count": len(tasks), "tasks": [task_to_dict(task) for task in tasks]}


def get_scheduled_task(ctx: RunContext[AgentDeps], task_id: str, include_deleted: bool = False) -> dict[str, object]:
    """查询单个定时任务详情。纯查询工具，不会修改调度系统。"""
    task = _get_service(ctx).get(task_id, include_deleted=include_deleted)
    return {"success": True, "task": task_to_dict(task)}


def list_scheduled_task_runs(ctx: RunContext[AgentDeps], task_id: str, limit: int = 20) -> dict[str, object]:
    """查看某个定时任务的运行记录。纯查询工具，不会修改调度系统。"""
    runs = _get_service(ctx).list_runs(task_id, limit=limit)
    return {"success": True, "count": len(runs), "runs": [run_to_dict(run) for run in runs]}


def read_schedule_inbox(ctx: RunContext[AgentDeps], unread_only: bool = True, limit: int = 20) -> dict[str, object]:
    """读取定时任务收件箱。纯查询工具，不会标记已读；需要标记已读时使用 mark_schedule_run_read。"""
    items = _get_service(ctx).read_inbox(unread_only=unread_only, limit=limit, mark_read=False)
    return {
        "success": True,
        "count": len(items),
        "items": [{"task": task_to_dict(task), "run": run_to_dict(run)} for task, run in items],
    }


def preview_scheduled_task_delivery(ctx: RunContext[AgentDeps], task_id: str) -> dict[str, object]:
    """预览定时任务投递目标。纯查询工具，不会修改投递策略。"""
    return {"success": True, "delivery": _get_service(ctx).preview_delivery(task_id)}


def update_scheduled_task(
    ctx: RunContext[AgentDeps],
    task_id: str,
    name: str | None = None,
    prompt: str | None = None,
    schedule_type: str | None = None,
    schedule_expr: str | None = None,
    timezone: str | None = None,
    role: str | None = None,
    session_id: str | None = None,
    timeout_seconds: int | None = None,
    execution_policy: dict[str, Any] | None = None,
) -> dict[str, object]:
    """更新定时任务。修改前请先用查询工具确认任务 ID；定时任务执行期间禁止调用。"""
    ensure_scheduler_mutation_allowed(ctx.deps.run_mode)
    updates: dict[str, Any] = {}
    for key, value in {
        "name": name,
        "prompt": prompt,
        "schedule_type": schedule_type,
        "schedule_expr": schedule_expr,
        "timezone": timezone,
        "role": role,
        "session_id": session_id,
        "timeout_seconds": timeout_seconds,
        "execution_policy": execution_policy,
    }.items():
        if value is not None:
            updates[key] = value
    if not updates:
        raise ValueError("update_scheduled_task 没有提供可更新字段。")
    task = _get_service(ctx).update(task_id, **updates)
    return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”已更新。"}


def update_scheduled_task_delivery(
    ctx: RunContext[AgentDeps],
    task_id: str,
    delivery_mode: str,
    delivery_targets: list[dict[str, Any]] | None = None,
    delivery_fallback: dict[str, Any] | None = None,
) -> dict[str, object]:
    """更新定时任务投递策略。修改前请先确认任务 ID；定时任务执行期间禁止调用。"""
    ensure_scheduler_mutation_allowed(ctx.deps.run_mode)
    task = _get_service(ctx).update_delivery(
        task_id,
        delivery_mode=delivery_mode,
        delivery_targets=delivery_targets,
        delivery_fallback=delivery_fallback,
    )
    return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”投递策略已更新。"}


def pause_scheduled_task(ctx: RunContext[AgentDeps], task_id: str) -> dict[str, object]:
    """暂停定时任务。修改前请先确认任务 ID；定时任务执行期间禁止调用。"""
    ensure_scheduler_mutation_allowed(ctx.deps.run_mode)
    task = _get_service(ctx).pause(task_id)
    return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”已暂停。"}


def resume_scheduled_task(ctx: RunContext[AgentDeps], task_id: str) -> dict[str, object]:
    """恢复定时任务。修改前请先确认任务 ID；定时任务执行期间禁止调用。"""
    ensure_scheduler_mutation_allowed(ctx.deps.run_mode)
    task = _get_service(ctx).resume(task_id)
    return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”已恢复。"}


def remove_scheduled_task(ctx: RunContext[AgentDeps], task_id: str) -> dict[str, object]:
    """删除定时任务。删除前请先确认任务 ID；定时任务执行期间禁止调用。"""
    ensure_scheduler_mutation_allowed(ctx.deps.run_mode)
    task = _get_service(ctx).remove(task_id)
    return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”已删除。"}


def mark_schedule_run_read(ctx: RunContext[AgentDeps], run_id: str) -> dict[str, object]:
    """将一条定时任务运行记录标记为已读。定时任务执行期间禁止调用。"""
    ensure_scheduler_mutation_allowed(ctx.deps.run_mode)
    run = _get_service(ctx).mark_read(run_id)
    return {"success": True, "run": run_to_dict(run)}


def manage_scheduled_task(
    ctx: RunContext[AgentDeps],
    action: str,
    task_id: str | None = None,
    name: str | None = None,
    prompt: str | None = None,
    schedule_type: str | None = None,
    schedule_expr: str | None = None,
    delay_seconds: int | None = None,
    timezone: str = "UTC",
    role: str | None = None,
    session_id: str | None = None,
    timeout_seconds: int | None = None,
    delivery_mode: str | None = None,
    delivery_targets: list[dict[str, Any]] | None = None,
    delivery_fallback: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
    include_deleted: bool = False,
    limit: int = 20,
    mark_read: bool = False,
) -> dict[str, object]:
    """兼容旧版聚合定时任务工具。新代码请使用显式读/写工具；本工具不再注册给模型。"""
    normalized = action.strip().lower()
    ensure_scheduler_action_allowed(ctx.deps.run_mode, normalized, mutates=normalized in _MUTATION_ACTIONS)

    if normalized == "create":
        if not name:
            raise ValueError("action=create 需要 name。")
        if not prompt:
            raise ValueError("action=create 需要 prompt。")
        if delay_seconds is not None:
            schedule_type = "once"
            schedule_expr = delayed_schedule_expr(delay_seconds)
        if not schedule_type or not schedule_expr:
            raise ValueError("action=create 需要 schedule_type/schedule_expr，或提供 delay_seconds。")
        return _create_task(
            ctx,
            name=name,
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_expr=schedule_expr,
            timezone=timezone,
            role=role,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
            delivery_mode=delivery_mode,
            delivery_targets=delivery_targets,
            delivery_fallback=delivery_fallback,
            execution_policy=execution_policy,
        )

    if normalized == "list":
        return list_scheduled_tasks(ctx, include_deleted=include_deleted)

    if normalized == "get":
        return get_scheduled_task(ctx, _require_task_id(normalized, task_id), include_deleted=include_deleted)

    if normalized == "update":
        return update_scheduled_task(
            ctx,
            _require_task_id(normalized, task_id),
            name=name,
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_expr=schedule_expr,
            timezone=timezone if timezone != "UTC" else None,
            role=role,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
            execution_policy=execution_policy,
        )

    if normalized == "update_delivery":
        if not delivery_mode:
            raise ValueError("action=update_delivery 需要 delivery_mode。")
        return update_scheduled_task_delivery(
            ctx,
            _require_task_id(normalized, task_id),
            delivery_mode=delivery_mode,
            delivery_targets=delivery_targets,
            delivery_fallback=delivery_fallback,
        )

    if normalized == "pause":
        return pause_scheduled_task(ctx, _require_task_id(normalized, task_id))

    if normalized == "resume":
        return resume_scheduled_task(ctx, _require_task_id(normalized, task_id))

    if normalized == "remove":
        return remove_scheduled_task(ctx, _require_task_id(normalized, task_id))

    if normalized == "list_runs":
        return list_scheduled_task_runs(ctx, _require_task_id(normalized, task_id), limit=limit)

    if normalized == "read_inbox":
        if mark_read:
            ensure_scheduler_action_allowed(ctx.deps.run_mode, normalized, mutates=True)
            raise RuntimeError("read_inbox 不再支持 mark_read=True；请先读取收件箱，再使用 mark_schedule_run_read(run_id=...) 标记指定运行记录。")
        return read_schedule_inbox(ctx, unread_only=not include_deleted, limit=limit)

    if normalized == "mark_read":
        return mark_schedule_run_read(ctx, _require_task_id(normalized, task_id))

    if normalized == "preview_delivery":
        return preview_scheduled_task_delivery(ctx, _require_task_id(normalized, task_id))

    raise ValueError(f"未知定时任务 action：{action}")


def create_delayed_task(
    ctx: RunContext[AgentDeps],
    prompt: str,
    delay_seconds: int,
    name: str = "延时任务",
    role: str | None = None,
) -> dict[str, object]:
    """创建一次性延时任务。用户说“稍后提醒/几分钟后回复/定时执行”时使用，不要用 sleep 或 run_command 等待。"""
    return _create_task(
        ctx,
        name=name,
        prompt=prompt,
        schedule_type="once",
        schedule_expr=delayed_schedule_expr(delay_seconds),
        role=role,
    )


def create_scheduled_task(
    ctx: RunContext[AgentDeps],
    name: str,
    prompt: str,
    schedule_type: str,
    schedule_expr: str,
    timezone: str = "UTC",
    role: str | None = None,
) -> dict[str, object]:
    """创建定时任务，支持 once、interval、cron。需要周期或 cron 定时执行时使用，不要用 shell sleep 循环。"""
    return _create_task(
        ctx,
        name=name,
        prompt=prompt,
        schedule_type=schedule_type,
        schedule_expr=schedule_expr,
        timezone=timezone,
        role=role,
    )
