"""长期记忆工具。"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import RunContext

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.memory.capture import REMEMBERED_THIS_TURN_KEY, _keywords
from openhachimi_agent.memory.conflicts import resolve_atom_conflict
from openhachimi_agent.memory.models import (
    MemoryAtom,
    MemoryScope,
    MemoryStability,
    MemoryType,
    default_expiry,
    normalize_memory_type,
)
from openhachimi_agent.memory.privacy import PrivacyGuard
from openhachimi_agent.memory.recall import get_memory_store
from openhachimi_agent.memory.scheduler import enqueue_consolidation


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
    if not ctx.deps.config.memory.enabled:
        return {"enabled": False, "items": []}
    store = get_memory_store(ctx.deps.config)
    results = store.list_memories(_scope(ctx), memory_type=memory_type, limit=limit, include_archived=False)
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


def _remembered_atom(scope: MemoryScope, memory_type: str, stability: str, decision: Any, tags: str | list[str] | None) -> MemoryAtom:
    """用户 / 模型显式要求记住的内容:置信度 0.95,按 stability 设定过期时间。"""
    stability_value = MemoryStability.STABLE if stability == "stable" else MemoryStability.SITUATIONAL
    valid_until, decay_at = default_expiry(stability_value)
    return MemoryAtom(
        memory_type=memory_type,
        content=decision.text,
        scope=scope,
        object=decision.text,
        keywords=_keywords(decision.text),
        tags=_normalize_tags(tags),
        confidence=0.95,
        stability=stability_value,
        sensitivity=decision.sensitivity,
        valid_until=valid_until,
        decay_at=decay_at,
    )


def _queue_embedding(store: Any, config: Any, memory_id: str, text: str) -> str:
    """排队计算向量,返回 embedding_status("queued" / "disabled")。"""
    if not config.memory.embedding.enabled:
        return "disabled"
    store.enqueue_unique_job(
        "embed_memory_item",
        {"item_id": memory_id, "level": "L1", "text": text, "model": config.memory.embedding.model},
        dedupe_key=f"embed:L1:{memory_id}",
    )
    return "queued"


def remember(
    ctx: RunContext[AgentDeps],
    content: str,
    memory_type: str = "user_preference",
    stability: str = "stable",
    tags: str | list[str] | None = None,
) -> dict[str, object]:
    """显式写入长期记忆。

    memory_type 取值:user_preference(偏好,默认)/ user_constraint(必须、禁止)/
    user_trait(用户身份、技能)/ project_fact(项目事实)/ project_decision(项目决策)/
    workflow(工作方式)/ task_reference(历史任务)。不认识的类型按 user_preference 存。
    """
    if not ctx.deps.config.memory.enabled:
        return {"enabled": False, "stored": False}
    # 归一类型:旧默认值 "fact" 等不在类型体系里,会让记忆在召回时被当成未知类型。
    memory_type = normalize_memory_type(memory_type, default=MemoryType.USER_PREFERENCE.value) or MemoryType.USER_PREFERENCE.value
    guard = PrivacyGuard(
        allow_secret_memory=ctx.deps.config.memory.privacy.allow_secret_memory,
        pii_redaction=ctx.deps.config.memory.privacy.pii_redaction,
    )
    decision = guard.should_store(content)
    if decision.action == "reject":
        return {"stored": False, "reason": decision.reason}

    atom = _remembered_atom(_scope(ctx), memory_type, stability, decision, tags)
    store = get_memory_store(ctx.deps.config)
    # 告诉本轮的记忆捕获:模型已经存过了,不要再对同一句"记住……"同步写一条
    if isinstance(ctx.deps.session_state, dict):
        ctx.deps.session_state[REMEMBERED_THIS_TURN_KEY] = True
    # 内容完全相同的记忆已存在时不再重复写入,直接返回已有记忆的 id
    conflict = resolve_atom_conflict(store, atom)
    if conflict.action == "dedupe":
        return {"stored": True, "id": conflict.winner_id, "memory_type": memory_type, "deduplicated": True}
    memory_id = store.add_atom(atom)
    embedding_status = _queue_embedding(store, ctx.deps.config, memory_id, atom.content)
    return {
        "stored": True,
        "id": memory_id,
        "memory_type": memory_type,
        "sensitivity": str(decision.sensitivity),
        "embedding_status": embedding_status,
    }


def forget_memory(ctx: RunContext[AgentDeps], query_or_ids: str, mode: str = "soft_delete") -> dict[str, object]:
    """删除或软删除长期记忆。

    参数 query_or_ids 接受两种形式:
    - 一个或多个 32 位 hex 记忆 ID,以逗号分隔(推荐),例如 ``"id1,id2,id3"``。
    - 一段搜索词(将走 FTS 模糊匹配命中前 20 条)。

    安全约束:
    - 通配符 ``*`` / ``%`` / 空字符串等"全量"指代不被支持,会被拒绝。
      如确需批量删除,请用 list_memory 列出后,以逗号分隔的明确 ID 列表传入。
    - hard_delete 模式不可恢复;soft_delete 仅标记为 deleted 状态。
    """
    if not ctx.deps.config.memory.enabled:
        return {"enabled": False, "deleted": 0}

    if not isinstance(query_or_ids, str):
        return {"deleted": 0, "error": "query_or_ids 必须是字符串"}
    stripped = query_or_ids.strip()
    if not stripped:
        return {"deleted": 0, "error": "query_or_ids 不能为空"}
    # 显式拒绝通配符 / 全量删除语义。批量删除应当用 list_memory 列举 ID 后逐个/逗号分隔传入。
    if stripped in {"*", "%", "all", "ALL"} or set(stripped) <= {"*", "%"}:
        return {
            "deleted": 0,
            "error": "拒绝通配符删除。请先用 list_memory 取得具体 ID,再以逗号分隔的 ID 列表传入 query_or_ids。",
        }

    store = get_memory_store(ctx.deps.config)
    scope = _scope(ctx)
    deleted = store.forget(scope, stripped, hard_delete=mode == "hard_delete")
    if deleted:
        # 删掉的内容可能已经被合并进主题块(L2)和用户画像(L3),排队重新合并把它清掉
        enqueue_consolidation(store, scope)
    return {"deleted": deleted, "mode": mode}


def update_memory(ctx: RunContext[AgentDeps], memory_id: str, content: str | None = None, status: str | None = None) -> dict[str, object]:
    """更新长期记忆。"""
    if not ctx.deps.config.memory.enabled:
        return {"enabled": False, "updated": False}
    store = get_memory_store(ctx.deps.config)
    if content:
        guard = PrivacyGuard(
            allow_secret_memory=ctx.deps.config.memory.privacy.allow_secret_memory,
            pii_redaction=ctx.deps.config.memory.privacy.pii_redaction,
        )
        decision = guard.should_store(content)
        if decision.action == "reject":
            return {"updated": False, "reason": decision.reason}
        embedding_status = "pending" if ctx.deps.config.memory.embedding.enabled else "disabled"
        updated = store.update_atom_content(memory_id, decision.text, embedding_status=embedding_status)
        if updated and ctx.deps.config.memory.embedding.enabled:
            store.enqueue_unique_job(
                "embed_memory_item",
                {
                    "item_id": memory_id,
                    "level": "L1",
                    "text": decision.text,
                    "model": ctx.deps.config.memory.embedding.model,
                },
                dedupe_key=f"embed:L1:{memory_id}",
            )
        return {"updated": updated, "id": memory_id, "embedding_status": "queued" if updated and ctx.deps.config.memory.embedding.enabled else embedding_status}
    if status == "deleted":
        return forget_memory(ctx, memory_id)
    return {"updated": False, "reason": "no_supported_update"}


def memory_stats(ctx: RunContext[AgentDeps]) -> dict[str, object]:
    """查看长期记忆统计。"""
    if not ctx.deps.config.memory.enabled:
        return {"enabled": False}
    return {"enabled": True, **get_memory_store(ctx.deps.config).stats()}
