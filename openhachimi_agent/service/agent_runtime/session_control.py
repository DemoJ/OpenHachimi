"""会话级中断与停止。

`AgentService` 持有 `_session_states` / `_running_tasks` / `_session_locks` /
`process_manager` 等运行态字段,这里提供接收 `service` 整体作参数的纯函数,
负责用户中断(stop)、资源回收、per-session 锁获取。`AgentService` 内对应方法
退化为薄壳。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from openhachimi_agent.core.identifiers import validate_session_id
from openhachimi_agent.transport.api_models import CommandResponse


logger = logging.getLogger(__name__)


async def interrupt_session_resources(service, session_id: str, reason: str = "interrupt") -> int:
    session_id = validate_session_id(session_id, allow_legacy=False)
    state = service._session_states.setdefault(session_id, {})
    state["cancel_requested"] = True
    state["cancel_reason"] = reason
    state["last_cancelled_at"] = time.time()
    return await _interrupt_session_resources(service, session_id)


async def stop_session(service, session_id: str) -> CommandResponse:
    session_id = validate_session_id(session_id, allow_legacy=False)
    logger.info("stop requested for session_id=%s", session_id)
    task = service._running_tasks.get(session_id)
    interrupted_count = await interrupt_session_resources(service, session_id, reason="user_stop")

    if task is not None:
        if not task.done():
            task.cancel()
            task.add_done_callback(_log_cancelled_task_result)
        return CommandResponse(
            message="已成功中断当前任务。",
            role=service.config.default_role_name,
            session_id=session_id,
        )
    if interrupted_count:
        return CommandResponse(
            message="已成功中断当前任务。",
            role=service.config.default_role_name,
            session_id=session_id,
        )
    return CommandResponse(
        message="当前没有正在运行的任务。",
        role=service.config.default_role_name,
        session_id=session_id,
    )


def _log_cancelled_task_result(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
        if exc is not None:
            logger.debug("cancelled task finished with error: %s", exc)


async def _interrupt_session_resources(service, session_id: str) -> int:
    terminate_session = getattr(service.process_manager, "terminate_session", None)
    if not callable(terminate_session):
        return 0
    try:
        count = await asyncio.to_thread(terminate_session, session_id)
    except Exception:
        logger.exception("failed to interrupt resources for session_id=%s", session_id)
        return 0
    logger.info("interrupted session resources session_id=%s process_count=%s", session_id, count)
    return count if isinstance(count, int) else 0


def get_session_lock(service, session_id: str) -> asyncio.Lock:
    lock = service._session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        service._session_locks[session_id] = lock
    return lock
