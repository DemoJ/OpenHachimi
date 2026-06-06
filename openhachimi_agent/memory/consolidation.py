"""长期记忆合并任务。"""

from __future__ import annotations

import math
import sqlite3
import time
from collections import defaultdict
from typing import Any

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.llm import run_memory_summary
from openhachimi_agent.memory.models import MemoryBlock, MemoryProfile, MemoryScope, MemoryStability, utc_now_iso
from openhachimi_agent.memory.prompts import MEMORY_EXTRACTION_PROMPT
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
    atoms = store.list_atoms_for_consolidation(scope, limit=atom_limit, min_confidence=min_atom_confidence)
    blocks_created, blocks_updated = consolidate_atoms_to_blocks(store, atoms, now=current, min_block_atoms=min_block_atoms, config=config)
    blocks = store.list_blocks_for_profile_consolidation(scope, limit=block_limit)
    profiles_created, profiles_updated = consolidate_blocks_to_profiles(store, blocks, now=current, config=config)
    return {
        "atoms_scanned": len(atoms),
        "atoms_expired": expired,
        "atoms_archived": archived,
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
        atom_ids = list(dict.fromkeys([*getattr(existing, "atom_ids", []), *(row["id"] for row in group_atoms)])) if existing else [row["id"] for row in group_atoms]
        keywords = _merge_json_lists(group_atoms, "keywords_json")
        entities = _merge_json_lists(group_atoms, "entities_json")
        tags = list(dict.fromkeys([block_type, topic_key, *_merge_json_lists(group_atoms, "tags_json")]))[:12]
        summary = _summarize_atoms(group_atoms, config=config)
        details = "\n".join(f"- {row['content']}" for row in group_atoms[:12])[:2000]
        confidence = min(0.98, sum(float(row["confidence"]) for row in group_atoms) / len(group_atoms) * (0.75 + 0.08 * math.log2(len(group_atoms) + 1)))
        coherence = _coherence(group_atoms)
        freshness = 1.0
        block_kwargs = dict(
            block_type=block_type,
            title=f"{block_type}: {topic_key}",
            summary=summary,
            details=details,
            scope=scope,
            atom_ids=atom_ids,
            entities=entities,
            keywords=keywords,
            tags=tags,
            confidence=confidence,
            coherence_score=coherence,
            freshness_score=freshness,
            last_consolidated_at=now,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            embedding_status=existing.embedding_status if existing else "pending",
        )
        block = MemoryBlock(id=existing.id, **block_kwargs) if existing else MemoryBlock(**block_kwargs)
        if existing is None:
            created += 1
        else:
            updated += 1
        store.add_block(block)
        store.link_block_atoms(block.id, atom_ids)
    return created, updated


def consolidate_blocks_to_profiles(store: MemoryStore, blocks: list[sqlite3.Row], *, now: str, config: AppConfig | None = None) -> tuple[int, int]:
    grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for block in blocks:
        grouped[(block["tenant_id"], block["user_id"], block["role_name"] or "")].append(block)
    created = 0
    updated = 0
    for (tenant_id, user_id, role_name), group_blocks in grouped.items():
        existing = store.get_active_profile(tenant_id, user_id, role_name, "user_profile")
        preferences = dict(existing.preferences) if existing else {}
        constraints = dict(existing.constraints) if existing else {}
        dislikes = dict(existing.dislikes) if existing else {}
        traits = dict(existing.traits) if existing else {}
        evidence: list[str] = list(existing.evidence_atom_ids) if existing else []
        for block in group_blocks:
            atom_ids = _load_json_array(block["atom_ids_json"])
            evidence.extend(atom_ids)
            entry = {"summary": block["summary"], "confidence": block["confidence"], "evidence": atom_ids[:20]}
            key = _profile_key(block)
            if block["block_type"] == "user_preference":
                preferences[key] = entry
            elif block["block_type"] == "user_constraint":
                constraints[key] = entry
            elif "dislike" in _load_json_array(block["tags_json"]):
                dislikes[key] = entry
            else:
                traits[key] = entry
        evidence = list(dict.fromkeys(evidence))[:100]
        summary = _profile_summary(preferences, constraints, dislikes, traits, config=config) if len(group_blocks) >= 3 else _profile_summary(preferences, constraints, dislikes, traits)
        profile = MemoryProfile(
            id=existing.id if existing else __import__("uuid").uuid4().hex,
            profile_type="user_profile",
            tenant_id=tenant_id,
            user_id=user_id,
            role_name=role_name or None,
            title="用户长期画像",
            summary=summary,
            traits=traits,
            preferences=preferences,
            constraints=constraints,
            dislikes=dislikes,
            evidence_atom_ids=evidence,
            confidence=min(0.95, 0.55 + 0.05 * len(evidence)),
            stability=MemoryStability.STABLE,
            last_reviewed_at=now,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        store.add_profile(profile)
        if existing:
            updated += 1
        else:
            created += 1
    return created, updated


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
    instruction = (
        MEMORY_EXTRACTION_PROMPT
        + "\n请基于证据生成长期记忆摘要。只输出 JSON：{\"summary\":\"...\"}。"
        + "block 摘要要去重、归纳共同主题、保留可执行偏好或项目事实；profile 摘要要形成稳定用户画像，不要编造证据。"
    )
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
