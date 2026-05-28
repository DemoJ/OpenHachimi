"""定时任务工具。"""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.scheduler.security import ensure_scheduler_action_allowed
from openhachimi_agent.scheduler.service import ScheduledTaskService, delayed_schedule_expr, run_to_dict, task_to_dict
from openhachimi_agent.scheduler.store import ScheduledTaskStore


_READ_ACTIONS = {"list", "get", "list_runs", "read_inbox", "preview_delivery"}


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
    """管理定时任务：创建、查询、修改、暂停、恢复、删除、立即运行、查看运行记录和收件箱。

    用户要求稍后提醒、定期执行、每天/每周/cron 任务时使用本工具，不要用 shell sleep、timeout 或循环等待。
    修改、暂停、删除、立即运行任务前，如果用户没有给出明确 ID，应先 action='list' 或 action='get' 确认唯一任务，不要猜 ID。
    默认 delivery_mode='origin'，即投递回创建任务的来源；“只保存/不通知”使用 delivery_mode='inbox' 或 'none'。
    定时任务无人值守执行期间禁止递归创建、修改、触发或删除定时任务。
    """
    normalized = action.strip().lower()
    ensure_scheduler_action_allowed(ctx.deps.run_mode, normalized)
    service = _get_service(ctx)

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

    if normalized == "list":
        tasks = service.list(include_deleted=include_deleted)
        return {"success": True, "count": len(tasks), "tasks": [task_to_dict(task) for task in tasks]}

    if normalized == "get":
        task = service.get(_require_task_id(normalized, task_id), include_deleted=include_deleted)
        return {"success": True, "task": task_to_dict(task)}

    if normalized == "update":
        updates: dict[str, Any] = {}
        for key, value in {
            "name": name,
            "prompt": prompt,
            "schedule_type": schedule_type,
            "schedule_expr": schedule_expr,
            "timezone": timezone if timezone != "UTC" else None,
            "role": role,
            "session_id": session_id,
            "timeout_seconds": timeout_seconds,
            "execution_policy": execution_policy,
        }.items():
            if value is not None:
                updates[key] = value
        if not updates:
            raise ValueError("action=update 没有提供可更新字段。")
        task = service.update(_require_task_id(normalized, task_id), **updates)
        return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”已更新。"}

    if normalized == "update_delivery":
        if not delivery_mode:
            raise ValueError("action=update_delivery 需要 delivery_mode。")
        task = service.update_delivery(
            _require_task_id(normalized, task_id),
            delivery_mode=delivery_mode,
            delivery_targets=delivery_targets,
            delivery_fallback=delivery_fallback,
        )
        return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”投递策略已更新。"}

    if normalized == "pause":
        task = service.pause(_require_task_id(normalized, task_id))
        return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”已暂停。"}

    if normalized == "resume":
        task = service.resume(_require_task_id(normalized, task_id))
        return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”已恢复。"}

    if normalized == "remove":
        task = service.remove(_require_task_id(normalized, task_id))
        return {"success": True, "task": task_to_dict(task), "message": f"定时任务“{task.name}”已删除。"}

    if normalized == "list_runs":
        runs = service.list_runs(_require_task_id(normalized, task_id), limit=limit)
        return {"success": True, "count": len(runs), "runs": [run_to_dict(run) for run in runs]}

    if normalized == "read_inbox":
        items = service.read_inbox(unread_only=not include_deleted, limit=limit, mark_read=mark_read)
        return {
            "success": True,
            "count": len(items),
            "items": [{"task": task_to_dict(task), "run": run_to_dict(run)} for task, run in items],
        }

    if normalized == "mark_read":
        run = service.mark_read(_require_task_id(normalized, task_id))
        return {"success": True, "run": run_to_dict(run)}

    if normalized == "preview_delivery":
        return {"success": True, "delivery": service.preview_delivery(_require_task_id(normalized, task_id))}

    raise ValueError(f"未知定时任务 action：{action}")


def create_delayed_task(
    ctx: RunContext[AgentDeps],
    prompt: str,
    delay_seconds: int,
    name: str = "延时任务",
    role: str | None = None,
) -> dict[str, object]:
    """创建一次性延时任务。用户说“稍后提醒/几分钟后回复/定时执行”时使用，不要用 sleep 或 run_command 等待。"""
    return manage_scheduled_task(
        ctx,
        action="create",
        name=name,
        prompt=prompt,
        delay_seconds=delay_seconds,
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
    return manage_scheduled_task(
        ctx,
        action="create",
        name=name,
        prompt=prompt,
        schedule_type=schedule_type,
        schedule_expr=schedule_expr,
        timezone=timezone,
        role=role,
    )
