"""长期记忆合并任务占位。"""

from __future__ import annotations

from openhachimi_agent.memory.store import MemoryStore


def consolidate_due_memories(store: MemoryStore) -> dict[str, int]:
    return {"blocks_updated": 0, "profiles_updated": 0}
