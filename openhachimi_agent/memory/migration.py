"""长期记忆迁移。"""

from __future__ import annotations

from pathlib import Path

from openhachimi_agent.memory.store import MemoryStore


def migrate_legacy_histories(store: MemoryStore, memory_dir: Path) -> dict[str, int]:
    return {"scanned": 0, "imported": 0}
