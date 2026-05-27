"""定时任务工具。"""

from __future__ import annotations

from datetime import timedelta

from pydantic_ai import RunContext

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.scheduler.models import utc_now
from openhachimi_agent.scheduler.store import ScheduledTaskStore


def _get_store(ctx: RunContext[AgentDeps]) -> ScheduledTaskStore:
    db_path = ctx.deps.config.scheduler.db_path
    if not ctx.deps.config.scheduler.enabled or db_path is None:
        raise RuntimeError("定时任务未启用。")
    return ScheduledTaskStore(db_path)


def create_delayed_task(
    ctx: RunContext[AgentDeps],
    prompt: str,
    delay_seconds: int,
    name: str = "延时任务",
    role: str | None = None,
) -> dict[str, object]:
    """创建一次性延时任务。用户说“稍后提醒/几分钟后回复/定时执行”时使用，不要用 sleep 或 run_command 等待。"""
    if delay_seconds <= 0:
        raise ValueError("delay_seconds 必须大于 0。")
    run_at = utc_now() + timedelta(seconds=delay_seconds)
    task = _get_store(ctx).create_task(
        name=name,
        prompt=prompt,
        schedule_type="once",
        schedule_expr=run_at.isoformat(),
        role=role,
        session_id=ctx.deps.session_id,
        metadata={"source": "agent_tool", "reply_to_session": ctx.deps.session_id},
    )
    return {
        "id": task.id,
        "name": task.name,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "message": "定时任务已创建，到点后会自动执行。",
    }


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
    task = _get_store(ctx).create_task(
        name=name,
        prompt=prompt,
        schedule_type=schedule_type,
        schedule_expr=schedule_expr,
        timezone_name=timezone,
        role=role,
        session_id=ctx.deps.session_id,
        metadata={"source": "agent_tool", "reply_to_session": ctx.deps.session_id},
    )
    return {
        "id": task.id,
        "name": task.name,
        "schedule_type": task.schedule_type.value,
        "schedule_expr": task.schedule_expr,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "message": "定时任务已创建，到点后会自动执行。",
    }
