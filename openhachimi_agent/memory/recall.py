"""长期记忆召回流程。"""

from __future__ import annotations

import logging

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.embeddings import EmbeddingProvider
from openhachimi_agent.memory.formatting import format_memory_context
from openhachimi_agent.memory.models import MemoryContext, MemoryScope
from openhachimi_agent.memory.store import MemoryStore

logger = logging.getLogger(__name__)


def get_memory_store(config: AppConfig) -> MemoryStore:
    db_path = config.memory.db_path or (config.memory_dir / "long_term_memory.sqlite3")
    return MemoryStore(db_path)


def _fuse_results(*groups: list) -> list:
    merged = {}
    for group in groups:
        for rank, item in enumerate(group, start=1):
            if item.id not in merged:
                merged[item.id] = item
            merged[item.id].score += 1.0 / (60 + rank)
            if item.source not in merged[item.id].source:
                merged[item.id].source = f"{merged[item.id].source}+{item.source}"
    results = list(merged.values())
    results.sort(key=lambda item: item.score, reverse=True)
    return results


def recall_memories(config: AppConfig, scope: MemoryScope, query: str) -> MemoryContext:
    if not config.memory.enabled or not query.strip():
        return MemoryContext(scope=scope, query=query)
    try:
        store = get_memory_store(config)
        limit = config.memory.recall.final_l1_top_k + config.memory.recall.final_l2_top_k + 4
        bm25_results = store.search(scope, query, limit=limit)
        vector_results = []
        degraded = False
        reason = ""
        if config.memory.embedding.enabled:
            embedding = EmbeddingProvider(config.memory.embedding).embed_sync(query)
            if embedding.degraded:
                degraded = True
                reason = embedding.reason
                logger.warning(
                    "memory vector recall skipped role=%s session_id=%s reason=%s",
                    scope.role_name,
                    scope.session_id,
                    embedding.reason,
                )
            else:
                vector_results = store.vector_search(scope, embedding.vector, limit=config.memory.recall.vector_top_k, model=config.memory.embedding.model)
                logger.info(
                    "memory vector recall succeeded role=%s session_id=%s vector_results=%d bm25_results=%d",
                    scope.role_name,
                    scope.session_id,
                    len(vector_results),
                    len(bm25_results),
                )
        results = _fuse_results(bm25_results, vector_results)[:limit]
        logger.info(
            "memory recall completed role=%s session_id=%s results=%d degraded=%s sources=%s",
            scope.role_name,
            scope.session_id,
            len(results),
            str(degraded).lower(),
            sorted({item.source for item in results}),
        )
        return MemoryContext(scope=scope, query=query, results=results, degraded=degraded, reason=reason)
    except Exception as exc:
        logger.warning("memory recall degraded role=%s session_id=%s: %s", scope.role_name, scope.session_id, exc)
        return MemoryContext(scope=scope, query=query, degraded=True, reason=str(exc))


def build_memory_context_text(config: AppConfig, context: MemoryContext | None) -> str:
    if context is None:
        return ""
    return format_memory_context(context, config.memory.recall.max_context_tokens)
