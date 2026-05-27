"""定时任务执行器。"""

from __future__ import annotations

import asyncio
import time

from typing import TYPE_CHECKING

from openhachimi_agent.scheduler.models import ScheduledTask
from openhachimi_agent.scheduler.store import ScheduledTaskStore

if TYPE_CHECKING:
    from openhachimi_agent.service.agent_service import AgentService


class ScheduledTaskRunner:
    def __init__(self, store: ScheduledTaskStore, service: "AgentService", *, default_timeout_seconds: int) -> None:
        self.store = store
        self.service = service
        self.default_timeout_seconds = default_timeout_seconds

    async def run_task(self, task: ScheduledTask, *, preserve_schedule: bool = False) -> object:
        session_id = task.session_id or f"schedule-{task.id}"
        if session_id in self.service._running_tasks:
            return await asyncio.to_thread(self.store.skip_task_run, task, error="session is already running")

        run = await asyncio.to_thread(self.store.prepare_task_run, task, preserve_schedule=preserve_schedule)
        started = time.perf_counter()
        timeout = task.timeout_seconds or self.default_timeout_seconds
        try:
            response = await asyncio.wait_for(
                self.service.send_message(task.prompt, task.role, session_id),
                timeout=timeout,
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            await asyncio.to_thread(self.store.complete_run, run.id, status="succeeded", output=response.output, duration_ms=duration_ms)
            return await asyncio.to_thread(self.store.get_run, run.id)
        except asyncio.TimeoutError:
            duration_ms = int((time.perf_counter() - started) * 1000)
            await asyncio.to_thread(self.store.complete_run, run.id, status="timeout", error=f"任务执行超过 {timeout}s", duration_ms=duration_ms)
            return await asyncio.to_thread(self.store.get_run, run.id)
        except asyncio.CancelledError:
            duration_ms = int((time.perf_counter() - started) * 1000)
            await asyncio.to_thread(self.store.complete_run, run.id, status="failed", error="任务执行被取消", duration_ms=duration_ms)
            raise
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            await asyncio.to_thread(self.store.complete_run, run.id, status="failed", error=str(exc), duration_ms=duration_ms)
            return await asyncio.to_thread(self.store.get_run, run.id)
