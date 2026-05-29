"""定时任务执行器。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from openhachimi_agent.scheduler.models import ScheduledRun, ScheduledTask
from openhachimi_agent.scheduler.security import scan_scheduled_prompt
from openhachimi_agent.scheduler.store import ScheduledTaskStore

if TYPE_CHECKING:
    from openhachimi_agent.scheduler.delivery import DeliverySenderRegistry
    from openhachimi_agent.service.agent_service import AgentService

logger = logging.getLogger(__name__)


def _execution_session_id(task: ScheduledTask) -> str:
    execution_context = task.execution_policy if isinstance(task.execution_policy, dict) else {}
    session_id = execution_context.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    return f"schedule-{task.id}"


def _build_channel_context(task: ScheduledTask) -> dict[str, Any]:
    origin = dict(task.origin or {})
    if origin:
        origin.setdefault("session_id", task.session_id or f"schedule-{task.id}")
        return origin
    return {
        "type": "local",
        "platform": "local",
        "session_id": task.session_id or f"schedule-{task.id}",
    }


def _build_scheduled_execution_prompt(task: ScheduledTask) -> str:
    """Wrap the stored task prompt so the model treats it as an already-due job."""
    return (
        "[IMPORTANT: 你正在执行一个已经到期的定时任务。]\n"
        "这不是用户新发来的普通请求，也不是让你创建、修改或安排定时任务。\n"
        "请执行下面的定时任务内容，并把最终结果作为本次任务输出。系统会负责按任务投递配置把最终结果发送给用户；"
        "不要自行创建新的定时任务，不要修改/暂停/恢复/删除/立即触发定时任务，也不要询问提醒时间。\n"
        "如果任务内容是提醒用户，请直接输出提醒消息。\n"
        "如果任务内容是生成早报、报告、摘要或检查结果，请直接完成内容生成。\n"
        "如果任务确实需要后续调度，请只在最终结果中说明需要用户在交互模式下确认。\n\n"
        f"定时任务 ID：{task.id}\n"
        f"定时任务名称：{task.name}\n"
        "定时任务内容：\n"
        f"{task.prompt}"
    )


class ScheduledTaskRunner:
    def __init__(
        self,
        store: ScheduledTaskStore,
        service: "AgentService",
        *,
        default_timeout_seconds: int,
        delivery_registry: "DeliverySenderRegistry | None" = None,
        config: Any = None,
    ) -> None:
        self.store = store
        self.service = service
        self.default_timeout_seconds = default_timeout_seconds
        self.delivery_registry = delivery_registry
        self.config = config

    async def run_task(self, task: ScheduledTask, *, preserve_schedule: bool = False) -> ScheduledRun | None:
        channel_context = _build_channel_context(task)
        session_id = _execution_session_id(task)
        if session_id in self.service._running_tasks:
            return await asyncio.to_thread(self.store.skip_task_run, task, error="session is already running")

        safety = scan_scheduled_prompt(task.prompt)
        execution_context = {"run_mode": "scheduled", "task_id": task.id, "task_name": task.name}

        if not safety.allowed:
            run = await asyncio.to_thread(
                self.store.prepare_task_run,
                task,
                preserve_schedule=preserve_schedule,
                execution_context=execution_context,
            )
            await asyncio.to_thread(
                self.store.complete_run,
                run.id,
                status="skipped",
                error=safety.reason or "定时任务提示词未通过安全检查。",
                duration_ms=0,
                safety_status="rejected",
                safety_error=safety.reason,
            )
            logger.warning("scheduled task safety rejected task_id=%s reason=%s", task.id, safety.reason)
            return await asyncio.to_thread(self.store.get_run, run.id)

        run = await asyncio.to_thread(
            self.store.prepare_task_run,
            task,
            preserve_schedule=preserve_schedule,
            execution_context=execution_context,
        )
        started = time.perf_counter()
        timeout = task.timeout_seconds or self.default_timeout_seconds
        try:
            response = await asyncio.wait_for(
                self.service.send_message(
                    _build_scheduled_execution_prompt(task),
                    task.role,
                    session_id,
                    run_mode="scheduled",
                    channel_context=channel_context,
                    scheduler_context={"task_id": task.id, "run_id": run.id},
                ),
                timeout=timeout,
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            await asyncio.to_thread(
                self.store.complete_run,
                run.id,
                status="succeeded",
                output=response.output,
                duration_ms=duration_ms,
                safety_status="allowed",
            )
            return await asyncio.to_thread(self.store.get_run, run.id)
        except asyncio.TimeoutError:
            duration_ms = int((time.perf_counter() - started) * 1000)
            await asyncio.to_thread(
                self.store.complete_run,
                run.id,
                status="timeout",
                error=f"任务执行超过 {timeout}s",
                duration_ms=duration_ms,
                safety_status="allowed",
            )
            return await asyncio.to_thread(self.store.get_run, run.id)
        except asyncio.CancelledError:
            duration_ms = int((time.perf_counter() - started) * 1000)
            await asyncio.to_thread(
                self.store.complete_run,
                run.id,
                status="failed",
                error="任务执行被取消",
                duration_ms=duration_ms,
                safety_status="allowed",
            )
            raise
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            await asyncio.to_thread(
                self.store.complete_run,
                run.id,
                status="failed",
                error=str(exc),
                duration_ms=duration_ms,
                safety_status="allowed",
            )
            return await asyncio.to_thread(self.store.get_run, run.id)
