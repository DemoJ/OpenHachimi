"""长期记忆 BM25 查询封装。"""

from __future__ import annotations

from openhachimi_agent.memory.models import MemoryScope, MemorySearchResult
from openhachimi_agent.memory.store import MemoryStore


def bm25_search(store: MemoryStore, scope: MemoryScope, query: str, limit: int) -> list[MemorySearchResult]:
    return store.search(scope, query, limit=limit)
