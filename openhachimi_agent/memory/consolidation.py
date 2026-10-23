"""长期记忆合并任务。"""

from __future__ import annotations

import dataclasses
import math
import sqlite3
import time
from collections import defaultdict
from typing import Any
from uuid import uuid4

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory._store.utils import _block_from_row
from openhachimi_agent.memory.llm import run_memory_summary
from openhachimi_agent.memory.models import (
    MemoryBlock,
    MemoryProfile,
    MemoryScope,
    MemoryStability,
    MemoryStatus,
    utc_now_iso,
)
from openhachimi_agent.memory.store import MemoryStore, _load_json_array


def consolidate_due_memories(
    store: MemoryStore,
    *,
    scope: MemoryScope | None = None,
    now: str | None = None,
    atom_limit: int = 200,
    min_block_atoms: int = 2,
    block_limit: int = 50,
    min_atom_confidence: float = 0.55,
    config: AppConfig | None = None,
) -> dict[str, int]:
    current = now or utc_now_iso()
    expired = store.expire_due_atoms(now=current)
    archived = store.archive_decayed_atoms(now=current)
    stale_tasks = store.archive_stale_task_references(now=current)
    stale_situational = store.archive_stale_situational_atoms(now=current)
    downgraded_stable = store.downgrade_stable_atoms(now=current)
    # 先清理已失效 atom 留下的旧内容,再合并新 atom:否则被覆盖 / 删除的记忆会一直留在 L2/L3 里
    blocks_archived, blocks_rebuilt = prune_stale_blocks(store, scope=scope, now=current, config=config)
    atoms = store.list_atoms_for_consolidation(scope, limit=atom_limit, min_confidence=min_atom_confidence)
    blocks_created, blocks_updated = consolidate_atoms_to_blocks(store, atoms, now=current, min_block_atoms=min_block_atoms, config=config)
    blocks = store.list_blocks_for_profile_consolidation(scope, limit=block_limit)
    profiles_created, profiles_updated, profiles_archived = consolidate_blocks_to_profiles(
        store, blocks, now=current, config=config, scope=scope
    )
    return {
        "blocks_archived": blocks_archived,
        "blocks_rebuilt": blocks_rebuilt,
        "profiles_archived": profiles_archived,
        "atoms_scanned": len(atoms),
        "atoms_expired": expired,
        "atoms_archived": archived,
        "stale_tasks_archived": stale_tasks,
        "stale_situational_archived": stale_situational,
        "stable_downgraded": downgraded_stable,
        "blocks_created": blocks_created,
        "blocks_updated": blocks_updated,
        "profiles_created": profiles_created,
        "profiles_updated": profiles_updated,
    }


def consolidate_atoms_to_blocks(
    store: MemoryStore,
    atoms: list[sqlite3.Row],
    *,
    now: str,
    min_block_atoms: int = 2,
    config: AppConfig | None = None,
) -> tuple[int, int]:
    groups: dict[tuple[str, str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for atom in atoms:
        block_type = _block_type(atom["memory_type"])
        topic_key = _topic_key(atom)
        groups[(atom["tenant_id"], atom["user_id"], atom["role_name"], block_type, topic_key)].append(atom)
    created = 0
    updated = 0
    for (tenant_id, user_id, role_name, block_type, topic_key), group_atoms in groups.items():
        if len(group_atoms) < min_block_atoms and block_type not in {"user_preference", "user_constraint"}:
            continue
        scope = MemoryScope(tenant_id=tenant_id, user_id=user_id, role_name=role_name, session_id=group_atoms[0]["session_id"], channel=group_atoms[0]["channel"])
        existing = store.get_active_block_by_topic(scope, block_type, topic_key)
        rows = list(group_atoms)
        if existing:
            # 本批之外仍有效的旧 atom 保留;已失效的不再带进新摘要
            group_ids = {row["id"] for row in group_atoms}
            rows += [
                row for row in store.get_atom_rows(existing.atom_ids)
                if row["status"] == MemoryStatus.ACTIVE.value and row["id"] not in group_ids
            ]
        _save_block(store, block_type=block_type, topic_key=topic_key, scope=scope, rows=rows, existing=existing, now=now, config=config)
        if existing is None:
            created += 1
        else:
            updated += 1
    return created, updated


# 清理时每次最多检查的 block 数。
_PRUNE_BLOCK_LIMIT = 500


def prune_stale_blocks(
    store: MemoryStore,
    *,
    scope: MemoryScope | None = None,
    now: str,
    config: AppConfig | None = None,
) -> tuple[int, int]:
    """清理关联了失效 atom(归档 / 过期 / 删除 / 被覆盖)的 L2 block。

    atom 全部失效则归档 block;部分失效则只用剩下的 atom 重建摘要。返回 (归档数, 重建数)。
    """
    archived = 0
    rebuilt = 0
    for block_row in store.list_blocks_for_profile_consolidation(scope, limit=_PRUNE_BLOCK_LIMIT):
        atom_ids = store.get_block_atom_ids(block_row)
        if not atom_ids:
            continue
        active = [row for row in store.get_atom_rows(atom_ids) if row["status"] == MemoryStatus.ACTIVE.value]
        if len(active) == len(atom_ids):
            continue
        if not active:
            store.archive_block(block_row["id"])
            archived += 1
            continue
        existing = _block_from_row(block_row)
        topic_key = existing.title.split(": ", 1)[-1]
        _save_block(store, block_type=existing.block_type, topic_key=topic_key, scope=existing.scope, rows=active, existing=existing, now=now, config=config)
        rebuilt += 1
    return archived, rebuilt


def _save_block(
    store: MemoryStore,
    *,
    block_type: str,
    topic_key: str,
    scope: MemoryScope,
    rows: list[sqlite3.Row],
    existing: MemoryBlock | None,
    now: str,
    config: AppConfig | None,
) -> None:
    """用给定的 atom 行生成(或整体覆盖)一个 block,关联表同步换成这些 atom。"""
    atom_ids = list(dict.fromkeys(row["id"] for row in rows))
    confidence = min(0.98, sum(float(row["confidence"]) for row in rows) / len(rows) * (0.75 + 0.08 * math.log2(len(rows) + 1)))
    block_kwargs = dict(
        block_type=block_type,
        title=f"{block_type}: {topic_key}",
        summary=_summarize_atoms(rows, config=config),
        details="\n".join(f"- {row['content']}" for row in rows[:12])[:2000],
        scope=scope,
        atom_ids=atom_ids,
        entities=_merge_json_lists(rows, "entities_json"),
        keywords=_merge_json_lists(rows, "keywords_json"),
        tags=list(dict.fromkeys([block_type, topic_key, *_merge_json_lists(rows, "tags_json")]))[:12],
        confidence=confidence,
        coherence_score=_coherence(rows),
        freshness_score=1.0,
        last_consolidated_at=now,
        created_at=existing.created_at if existing else now,
        updated_at=now,
        embedding_status=existing.embedding_status if existing else "pending",
    )
    block = MemoryBlock(id=existing.id, **block_kwargs) if existing else MemoryBlock(**block_kwargs)
    store.add_block(block)
    store.replace_block_atoms(block.id, atom_ids)


def consolidate_blocks_to_profiles(
    store: MemoryStore,
    blocks: list[sqlite3.Row],
    *,
    now: str,
    config: AppConfig | None = None,
    scope: MemoryScope | None = None,
) -> tuple[int, int, int]:
    """按当前 active block 重建用户画像。返回 (新建数, 更新数, 归档数)。

    每次都从当前 block 重新生成,不再合并旧画像里的条目:旧条目可能来自已失效的记忆。
    指定 scope 且该作用域已没有任何 active block 时,归档旧画像。
    """
    grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for block in blocks:
        grouped[(block["tenant_id"], block["user_id"], block["role_name"] or "")].append(block)
    created = 0
    updated = 0
    archived = 0
    for (tenant_id, user_id, role_name), group_blocks in grouped.items():
        existing = store.get_active_profile(tenant_id, user_id, role_name, "user_profile")
        store.add_profile(_build_profile(tenant_id, user_id, role_name, group_blocks, existing, now=now, config=config))
        if existing:
            updated += 1
        else:
            created += 1
    if scope is not None and not grouped:
        existing = store.get_active_profile(scope.tenant_id, scope.user_id, scope.role_name, "user_profile")
        if existing:
            store.add_profile(dataclasses.replace(existing, status=MemoryStatus.ARCHIVED, updated_at=now))
            archived += 1
    return created, updated, archived


def _build_profile(
    tenant_id: str,
    user_id: str,
    role_name: str,
    blocks: list[sqlite3.Row],
    existing: MemoryProfile | None,
    *,
    now: str,
    config: AppConfig | None,
) -> MemoryProfile:
    sections: dict[str, dict[str, Any]] = {"preferences": {}, "constraints": {}, "dislikes": {}, "traits": {}}
    evidence: list[str] = []
    for block in blocks:
        atom_ids = _load_json_array(block["atom_ids_json"])
        evidence.extend(atom_ids)
        entry = {"summary": block["summary"], "confidence": block["confidence"], "evidence": atom_ids[:20]}
        sections[_profile_section(block)][_profile_key(block)] = entry
    evidence = list(dict.fromkeys(evidence))[:100]
    summary_config = config if len(blocks) >= 3 else None
    summary = _profile_summary(
        sections["preferences"], sections["constraints"], sections["dislikes"], sections["traits"], config=summary_config
    )
    return MemoryProfile(
        id=existing.id if existing else uuid4().hex,
        profile_type="user_profile",
        tenant_id=tenant_id,
        user_id=user_id,
        role_name=role_name or None,
        title="用户长期画像",
        summary=summary,
        traits=sections["traits"],
        preferences=sections["preferences"],
        constraints=sections["constraints"],
        dislikes=sections["dislikes"],
        evidence_atom_ids=evidence,
        confidence=min(0.95, 0.55 + 0.05 * len(evidence)),
        stability=MemoryStability.STABLE,
        last_reviewed_at=now,
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )


def _profile_section(block: sqlite3.Row) -> str:
    if block["block_type"] == "user_preference":
        return "preferences"
    if block["block_type"] == "user_constraint":
        return "constraints"
    if "dislike" in _load_json_array(block["tags_json"]):
        return "dislikes"
    return "traits"


def _block_type(memory_type: str) -> str:
    return {
        "preference": "user_preference",
        "constraint": "user_constraint",
        "project_context": "project_context",
        "decision": "decision_log",
        "fact": "user_fact",
    }.get(memory_type, memory_type or "user_fact")


def _topic_key(atom: sqlite3.Row) -> str:
    for field in ("tags_json", "keywords_json", "entities_json"):
        values = _load_json_array(atom[field])
        if values:
            return values[0][:32]
    return str(atom["normalized_content"] or atom["content"])[:24]


def _merge_json_lists(rows: list[sqlite3.Row], field: str) -> list[str]:
    values: list[str] = []
    for row in rows:
        values.extend(_load_json_array(row[field]))
    return list(dict.fromkeys(values))[:20]


def _summarize_atoms(rows: list[sqlite3.Row], *, config: AppConfig | None = None) -> str:
    contents = [str(row["content"]).strip() for row in sorted(rows, key=lambda item: float(item["confidence"]), reverse=True)]
    fallback = "；".join(contents)[:500]
    if len(rows) < 3:
        return fallback
    return _llm_summarize("block", {"atoms": contents[:20]}, config) or fallback


def _coherence(rows: list[sqlite3.Row]) -> float:
    keyword_sets = [set(_load_json_array(row["keywords_json"])) for row in rows]
    unique = set().union(*keyword_sets) if keyword_sets else set()
    if not unique:
        return 0.6
    shared = set.intersection(*keyword_sets) if len(keyword_sets) > 1 else unique
    return min(1.0, 0.5 + 0.5 * len(shared) / len(unique))


def _profile_key(block: sqlite3.Row) -> str:
    keywords = _load_json_array(block["keywords_json"])
    if keywords:
        return keywords[0][:32]
    return str(block["title"]).split(":", 1)[-1].strip()[:32]


def _profile_summary(
    preferences: dict[str, Any],
    constraints: dict[str, Any],
    dislikes: dict[str, Any],
    traits: dict[str, Any],
    *,
    config: AppConfig | None = None,
) -> str:
    parts = []
    if preferences:
        parts.append("用户长期偏好：" + "；".join(value["summary"] for value in list(preferences.values())[:3]))
    if constraints:
        parts.append("约束：" + "；".join(value["summary"] for value in list(constraints.values())[:3]))
    if dislikes:
        parts.append("不喜欢：" + "；".join(value["summary"] for value in list(dislikes.values())[:3]))
    if traits and not parts:
        parts.append("用户画像：" + "；".join(value["summary"] for value in list(traits.values())[:3]))
    fallback = "；".join(parts)[:600] or "暂无稳定画像。"
    evidence_count = len(preferences) + len(constraints) + len(dislikes) + len(traits)
    if evidence_count < 3:
        return fallback
    payload = {"preferences": preferences, "constraints": constraints, "dislikes": dislikes, "traits": traits}
    return _llm_summarize("profile", payload, config) or fallback


def _llm_summarize(kind: str, payload: dict[str, Any], config: AppConfig | None) -> str:
    instruction = load_system_prompt("memory/summary")
    started = time.perf_counter()
    try:
        output = run_memory_summary(
            config,
            system_prompt=instruction,
            payload={"kind": kind, "evidence": payload},
        )
        if output is None:
            return ""
        return str(output.summary or "").strip()[:800]
    except Exception:
        return ""
    finally:
        _ = int((time.perf_counter() - started) * 1000)
