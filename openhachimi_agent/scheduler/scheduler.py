"""定时任务后台调度循环。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from openhachimi_agent.scheduler.models import ScheduledRun, ScheduledTask
from openhachimi_agent.scheduler.runner import ScheduledTaskRunner
from openhachimi_agent.scheduler.store import ScheduledTaskStore
from openhachimi_agent.service.agent_service import AgentService

logger = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(
        self,
        store: ScheduledTaskStore,
        service: AgentService,
        *,
        poll_interval_seconds: int,
        max_concurrency: int,
        default_timeout_seconds: int,
        claim_lock_seconds: int,
        delivery_registry: Any = None,
        config: Any = None,
        on_run_complete: Callable[[ScheduledTask, ScheduledRun], Awaitable[None] | None] | None = None,
    ) -> None:
        self.store = store
        self.service = service
        self.poll_interval_seconds = poll_interval_seconds
        self.max_concurrency = max_concurrency
        self.claim_lock_seconds = claim_lock_seconds
        self.delivery_registry = delivery_registry
        self.config = config
        self.runner = ScheduledTaskRunner(
            store,
            service,
            default_timeout_seconds=default_timeout_seconds,
            delivery_registry=delivery_registry,
            config=config,
        )
        self.on_run_complete = on_run_complete
        self.running = False
        self._task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._active_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("task scheduler started")

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._active_tasks:
            for task in self._active_tasks:
                task.cancel()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        logger.info("task scheduler stopped")

    async def _run_loop(self) -> None:
        while self.running:
            await self.run_once()
            await asyncio.sleep(self.poll_interval_seconds)

    async def run_once(self) -> dict[str, int]:
        capacity = max(0, self.max_concurrency - len(self._active_tasks))
        if capacity <= 0:
            return {"claimed": 0, "started": 0}
        tasks = await asyncio.to_thread(self.store.claim_due_tasks, capacity, self.claim_lock_seconds)
        for task in tasks:
            active = asyncio.create_task(self._run_claimed_task(task))
            self._active_tasks.add(active)
            active.add_done_callback(self._active_tasks.discard)
        return {"claimed": len(tasks), "started": len(tasks)}

    async def run_task_now(self, task: ScheduledTask, *, preserve_schedule: bool = True) -> ScheduledRun | None:
        """立即运行一个任务（手动触发），并触发 on_run_complete 回调。"""
        try:
            run = await self.runner.run_task(task, preserve_schedule=preserve_schedule)
        except Exception as exc:
            logger.exception("manual scheduled task failed task_id=%s", task.id)
            await asyncio.to_thread(self.store.release_task, task.id, status="failed", error=str(exc))
            return None
        if self.on_run_complete and run is not None:
            result = self.on_run_complete(task, run)
            if inspect.isawaitable(result):
                await result
        return run

    async def _run_claimed_task(self, task: ScheduledTask) -> None:
        async with self._semaphore:
            try:
                run = await self.runner.run_task(task)
                if self.on_run_complete and run is not None:
                    result = self.on_run_complete(task, run)
                    if inspect.isawaitable(result):
                        await result
            except Exception as exc:
                logger.exception("scheduled task failed task_id=%s", task.id)
                await asyncio.to_thread(self.store.release_task, task.id, status="failed", error=str(exc))
