"""长期记忆召回流程。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.embeddings import EmbeddingProvider
from openhachimi_agent.memory.formatting import format_memory_context
from openhachimi_agent.memory.models import MemoryContext, MemoryScope, MemorySearchResult
from openhachimi_agent.memory.store import MemoryStore

logger = logging.getLogger(__name__)
_STORE_CACHE: dict[Path, MemoryStore] = {}


def get_memory_store(config: AppConfig) -> MemoryStore:
    db_path = (config.memory.db_path or (config.memory_dir / "long_term_memory.sqlite3")).resolve()
    store = _STORE_CACHE.get(db_path)
    if store is None:
        store = MemoryStore(db_path)
        _STORE_CACHE[db_path] = store
    return store


def fuse_results(groups: list[list[MemorySearchResult]], *, rrf_k: int = 60, level_weights: dict[str, float] | None = None) -> list[MemorySearchResult]:
    weights = level_weights or {"L1": 1.0, "L2": 1.08, "L3": 1.15}
    merged: dict[str, MemorySearchResult] = {}
    for group in groups:
        for rank, item in enumerate(group, start=1):
            if item.id not in merged:
                merged[item.id] = item
                merged[item.id].score *= weights.get(item.level, 1.0)
            merged[item.id].score += 1.0 / (rrf_k + rank)
            if item.source not in merged[item.id].source:
                merged[item.id].source = f"{merged[item.id].source}+{item.source}"
    results = [result for result in (apply_recall_decay(item) for item in merged.values()) if result is not None]
    results.sort(key=lambda item: item.score, reverse=True)
    return results


def _fuse_results(*groups: list[MemorySearchResult]) -> list[MemorySearchResult]:
    return fuse_results(list(groups))


def apply_recall_decay(result: MemorySearchResult, *, now: str | None = None) -> MemorySearchResult | None:
    current = _parse_time(now) if now else datetime.now().astimezone()
    valid_until = _parse_time(result.metadata.get("valid_until"))
    if valid_until and valid_until <= current:
        return None
    decay_at = _parse_time(result.metadata.get("decay_at"))
    if decay_at and decay_at <= current:
        stability = str(result.metadata.get("stability", "situational"))
        if stability == "ephemeral":
            result.score *= 0.2
        elif stability == "situational":
            result.score *= 0.55
        else:
            result.score *= 0.85
    return result


# v2: 这些 memory_type 不应从 L1 召回,避免历史问句/模型回答/定时调度 payload
# 被当成长期事实注入 system prompt
_EXCLUDED_L1_TYPES = {"conversation_context", "scheduler_payload"}


def select_by_level_budget(
    results: list[MemorySearchResult],
    *,
    final_l1_top_k: int,
    final_l2_top_k: int,
    include_l3_profile: bool,
) -> list[MemorySearchResult]:
    selected: list[MemorySearchResult] = []
    l1 = [
        item for item in results
        if item.level == "L1" and item.memory_type not in _EXCLUDED_L1_TYPES
    ][:final_l1_top_k]
    l2 = [item for item in results if item.level == "L2"][:final_l2_top_k]
    selected.extend(l1)
    selected.extend(l2)
    if include_l3_profile:
        selected.extend([item for item in results if item.level == "L3"][:1])
    selected.sort(key=lambda item: item.score, reverse=True)
    return selected


def recall_memories(config: AppConfig, scope: MemoryScope, query: str) -> MemoryContext:
    if not config.memory.enabled or not query.strip():
        return MemoryContext(scope=scope, query=query)
    try:
        store = get_memory_store(config)
        bm25_results = store.search(scope, query, limit=config.memory.recall.bm25_top_k, touch_results=False)
        vector_results: list[MemorySearchResult] = []
        degraded = False
        reason = ""
        if config.memory.embedding.enabled:
            embedding = EmbeddingProvider(config.memory.embedding).embed_sync(query)
            if embedding.degraded:
                degraded = True
                reason = embedding.reason
                logger.warning("memory vector recall skipped role=%s session_id=%s reason=%s", scope.role_name, scope.session_id, embedding.reason)
            else:
                vector_results = store.vector_search(scope, embedding.vector, limit=config.memory.recall.vector_top_k, model=config.memory.embedding.model, levels=("L1", "L2"))
                logger.info(
                    "memory vector recall succeeded role=%s session_id=%s vector_results=%d bm25_results=%d",
                    scope.role_name,
                    scope.session_id,
                    len(vector_results),
                    len(bm25_results),
                )
        fused = fuse_results([bm25_results, vector_results], rrf_k=config.memory.recall.rrf_k)
        results = select_by_level_budget(
            fused,
            final_l1_top_k=config.memory.recall.final_l1_top_k,
            final_l2_top_k=config.memory.recall.final_l2_top_k,
            include_l3_profile=config.memory.recall.include_l3_profile,
        )
        store.touch([item.id for item in results])
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


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
