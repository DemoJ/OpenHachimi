"""长期记忆工具。"""

from __future__ import annotations

import json

from pydantic_ai import RunContext

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.memory.models import MemoryAtom, MemoryScope, MemoryStability
from openhachimi_agent.memory.privacy import PrivacyGuard
from openhachimi_agent.memory.recall import get_memory_store


def _normalize_tags(tags: str | list[str] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        text = tags.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]
    return [str(item).strip() for item in tags if str(item).strip()]


def _scope(ctx: RunContext[AgentDeps]) -> MemoryScope:
    return ctx.deps.memory_scope or MemoryScope(
        role_name=ctx.deps.config.default_role_name,
        session_id=ctx.deps.session_id,
    )


def search_memory(ctx: RunContext[AgentDeps], query: str, top_k: int = 10, include_archived: bool = False) -> dict[str, object]:
    """搜索长期记忆。"""
    if not ctx.deps.config.memory.enabled:
        return {"enabled": False, "items": []}
    store = get_memory_store(ctx.deps.config)
    results = store.search(_scope(ctx), query, limit=top_k, include_archived=include_archived)
    return {
        "enabled": True,
        "items": [
            {
                "id": item.id,
                "level": item.level,
                "type": item.memory_type,
                "content": item.content,
                "confidence": item.confidence,
                "score": item.score,
                "updated_at": item.updated_at,
            }
            for item in results
        ],
    }


def list_memory(ctx: RunContext[AgentDeps], memory_type: str | None = None, limit: int = 20) -> dict[str, object]:
    """列出长期记忆。"""
    query = memory_type or "用户 项目 偏好 记忆 背景"
    return search_memory(ctx, query, top_k=limit, include_archived=False)


def remember(
    ctx: RunContext[AgentDeps],
    content: str,
    memory_type: str = "fact",
    stability: str = "stable",
    tags: str | list[str] | None = None,
) -> dict[str, object]:
    """显式写入长期记忆。"""
    if not ctx.deps.config.memory.enabled:
        return {"enabled": False, "stored": False}
    guard = PrivacyGuard(
        allow_secret_memory=ctx.deps.config.memory.privacy.allow_secret_memory,
        pii_redaction=ctx.deps.config.memory.privacy.pii_redaction,
    )
    decision = guard.should_store(content)
    if decision.action == "reject":
        return {"stored": False, "reason": decision.reason}

    scope = _scope(ctx)
    atom = MemoryAtom(
        memory_type=memory_type,
        content=decision.text,
        scope=scope,
        object=decision.text,
        keywords=[part[:32] for part in decision.text.split()[:12]],
        tags=_normalize_tags(tags),
        confidence=0.95,
        stability=MemoryStability.STABLE if stability == "stable" else MemoryStability.SITUATIONAL,
        sensitivity=decision.sensitivity,
    )
    store = get_memory_store(ctx.deps.config)
    memory_id = store.add_atom(atom)
    return {"stored": True, "id": memory_id, "sensitivity": str(decision.sensitivity)}


def forget_memory(ctx: RunContext[AgentDeps], query_or_ids: str, mode: str = "soft_delete") -> dict[str, object]:
    """删除或软删除长期记忆。"""
    if not ctx.deps.config.memory.enabled:
        return {"enabled": False, "deleted": 0}
    store = get_memory_store(ctx.deps.config)
    deleted = store.forget(_scope(ctx), query_or_ids, hard_delete=mode == "hard_delete")
    return {"deleted": deleted, "mode": mode}


def update_memory(ctx: RunContext[AgentDeps], memory_id: str, content: str | None = None, status: str | None = None) -> dict[str, object]:
    """更新长期记忆。当前支持通过先删除再 remember 进行内容修正。"""
    if content:
        forget_memory(ctx, memory_id)
        stored = remember(ctx, content, memory_type="correction")
        return {"updated": bool(stored.get("stored")), "replacement": stored}
    if status == "deleted":
        return forget_memory(ctx, memory_id)
    return {"updated": False, "reason": "no_supported_update"}


def memory_stats(ctx: RunContext[AgentDeps]) -> dict[str, object]:
    """查看长期记忆统计。"""
    if not ctx.deps.config.memory.enabled:
        return {"enabled": False}
    return {"enabled": True, **get_memory_store(ctx.deps.config).stats()}
