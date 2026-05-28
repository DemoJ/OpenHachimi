"""定时任务业务服务。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from openhachimi_agent.scheduler.models import ScheduleType, ScheduledRun, ScheduledTask, utc_now
from openhachimi_agent.scheduler.security import scan_scheduled_prompt
from openhachimi_agent.scheduler.store import ScheduledTaskStore


DEFAULT_FALLBACK = {"enabled": True, "mode": "inbox", "targets": [{"type": "inbox", "box": "default"}], "on": ["resolve_failed", "send_failed"]}


class ScheduledTaskService:
    def __init__(self, store: ScheduledTaskStore) -> None:
        self.store = store

    def create(
        self,
        *,
        name: str,
        prompt: str,
        schedule_type: str,
        schedule_expr: str,
        timezone: str = "UTC",
        role: str | None = None,
        session_id: str | None = None,
        timeout_seconds: int | None = None,
        origin: dict[str, Any] | None = None,
        delivery_mode: str = "origin",
        delivery_targets: list[dict[str, Any]] | None = None,
        delivery_fallback: dict[str, Any] | None = None,
        execution_policy: dict[str, Any] | None = None,
    ) -> ScheduledTask:
        safety = scan_scheduled_prompt(prompt)
        if not safety.allowed:
            raise ValueError(safety.reason or "定时任务提示词未通过安全检查。")
        return self.store.create_task(
            name=name,
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_expr=schedule_expr,
            timezone_name=timezone,
            role=role,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
            origin=origin or {},
            delivery_mode=delivery_mode,
            delivery_targets=delivery_targets or [],
            delivery_fallback=delivery_fallback or DEFAULT_FALLBACK,
            execution_policy=execution_policy or {},
        )

    def list(self, *, include_deleted: bool = False) -> list[ScheduledTask]:
        return self.store.list_tasks(include_deleted=include_deleted)

    def get(self, task_id_or_name: str, *, include_deleted: bool = False) -> ScheduledTask:
        task = self.store.resolve_task_ref(task_id_or_name, include_deleted=include_deleted)
        if task is None:
            raise KeyError(task_id_or_name)
        return task

    def update(self, task_id_or_name: str, **updates: Any) -> ScheduledTask:
        task = self.get(task_id_or_name, include_deleted=True)
        if "prompt" in updates and updates["prompt"] is not None:
            safety = scan_scheduled_prompt(str(updates["prompt"]))
            if not safety.allowed:
                raise ValueError(safety.reason or "定时任务提示词未通过安全检查。")
        return self.store.update_task(task.id, **updates)

    def update_delivery(
        self,
        task_id_or_name: str,
        *,
        delivery_mode: str,
        delivery_targets: list[dict[str, Any]] | None = None,
        delivery_fallback: dict[str, Any] | None = None,
    ) -> ScheduledTask:
        updates: dict[str, Any] = {"delivery_mode": delivery_mode}
        if delivery_targets is not None:
            updates["delivery_targets"] = delivery_targets
        if delivery_fallback is not None:
            updates["delivery_fallback"] = delivery_fallback
        return self.update(task_id_or_name, **updates)

    def pause(self, task_id_or_name: str, *, reason: str | None = None) -> ScheduledTask:
        task = self.get(task_id_or_name, include_deleted=True)
        return self.store.pause_task(task.id, reason=reason)

    def resume(self, task_id_or_name: str) -> ScheduledTask:
        task = self.get(task_id_or_name, include_deleted=True)
        return self.store.resume_task(task.id)

    def remove(self, task_id_or_name: str) -> ScheduledTask:
        task = self.get(task_id_or_name, include_deleted=True)
        return self.store.delete_task(task.id)

    def list_runs(self, task_id_or_name: str, *, limit: int = 20) -> list[ScheduledRun]:
        task = self.get(task_id_or_name, include_deleted=True)
        return self.store.list_runs(task.id, limit)

    def get_run(self, run_id: str) -> ScheduledRun:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def read_inbox(self, *, unread_only: bool = True, limit: int = 20, mark_read: bool = False) -> list[tuple[ScheduledTask, ScheduledRun]]:
        items = self.store.list_inbox_runs(unread_only=unread_only, limit=limit)
        if mark_read:
            for _task, run in items:
                self.store.mark_run_read(run.id)
        return items

    def mark_read(self, run_id: str) -> ScheduledRun:
        self.store.mark_run_read(run_id)
        return self.get_run(run_id)

    def preview_delivery(self, task_id_or_name: str) -> dict[str, Any]:
        task = self.get(task_id_or_name, include_deleted=True)
        return {"mode": task.delivery_mode, "targets": task.delivery_targets, "fallback": task.delivery_fallback, "origin": task.origin}


def delayed_schedule_expr(delay_seconds: int) -> str:
    if delay_seconds <= 0:
        raise ValueError("delay_seconds 必须大于 0。")
    return (utc_now() + timedelta(seconds=delay_seconds)).isoformat()


def task_to_dict(task: ScheduledTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "name": task.name,
        "prompt": task.prompt,
        "schedule_type": task.schedule_type.value,
        "schedule_expr": task.schedule_expr,
        "timezone": task.timezone,
        "status": task.status,
        "enabled": task.enabled,
        "role": task.role,
        "session_id": task.session_id,
        "timeout_seconds": task.timeout_seconds,
        "origin": task.origin,
        "delivery_mode": task.delivery_mode,
        "delivery_targets": task.delivery_targets,
        "delivery_fallback": task.delivery_fallback,
        "execution_policy": task.execution_policy,
        "safety_status": task.safety_status,
        "safety_error": task.safety_error,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "last_status": task.last_status,
        "last_error": task.last_error,
        "last_delivery_status": task.last_delivery_status,
        "last_delivery_error": task.last_delivery_error,
        "running": task.running,
    }


def run_to_dict(run: ScheduledRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "task_id": run.task_id,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "output": run.output,
        "error": run.error,
        "duration_ms": run.duration_ms,
        "delivery_status": run.delivery_status,
        "delivery_targets": run.delivery_targets,
        "delivery_results": run.delivery_results,
        "delivery_error": run.delivery_error,
        "delivered_at": run.delivered_at.isoformat() if run.delivered_at else None,
        "read_at": run.read_at.isoformat() if run.read_at else None,
        "safety_status": run.safety_status,
        "safety_error": run.safety_error,
        "execution_context": run.execution_context,
    }
