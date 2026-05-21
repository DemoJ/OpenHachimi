"""长期记忆后台任务调度。"""

from __future__ import annotations

import logging

from openhachimi_agent.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryScheduler:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.running = False

    async def start(self) -> None:
        self.running = True
        logger.debug("memory scheduler started")

    async def stop(self) -> None:
        self.running = False
        logger.debug("memory scheduler stopped")
