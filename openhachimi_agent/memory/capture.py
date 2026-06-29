"""长期记忆捕获与抽取。"""

from __future__ import annotations

import json
import logging
from typing import Any

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.conflicts import resolve_atom_conflict
from openhachimi_agent.memory.embeddings import EmbeddingProvider
from openhachimi_agent.memory.models import MemoryAtom, MemoryScope, MemoryStability, MemoryTurn
from openhachimi_agent.memory.privacy import PrivacyGuard
from openhachimi_agent.memory.recall import get_memory_store

logger = logging.getLogger(__name__)


# user_message 中可能因历史 bug 携带的污染特征,用于在抽取前剥离,避免它们
# 进入长期记忆。被污染的内容大多由旧版 runtime_context.build_volatile_prefix
# 引入(时间块 / <memory-context> / <skill> SKILL.md 全文)。
_VOLATILE_PREFIX_STRIP_MARKERS = (
    "[系统环境] 当前真实时间:",
    "<memory-context>",
    "<skill name=",
    "[System] 以下是基于当前任务意图自动匹配到的专家技能指令",
    "[IMPORTANT: 你正在执行一个已经到期的定时任务",
    "请执行以下用户任务。必须遵守 TaskFrame",
    "TaskFrame：{",
    '"user_request":',
)


def _keywords(text: str) -> list[str]:
    words = []
    for part in "".join(char if char.isalnum() or "一" <= char <= "鿿" else " " for char in text).split():
        if len(part) >= 2:
            words.append(part[:32])
    return list(dict.fromkeys(words))[:12]


def _looks_memorable(user_message: str) -> bool:
    markers = ["记住", "以后", "偏好", "喜欢", "不喜欢", "习惯", "要求", "纠正", "不要", "项目", "背景", "决定", "remember", "prefer", "preference", "like", "dislike"]
    return any(marker in user_message for marker in markers)


def _looks_like_scheduler_payload(text: str) -> bool:
    """判断一段文本是否是定时任务系统下发的执行 payload。

    旧版代码会把 ``runtime/scheduled_task_execution.md`` 渲染出的整段提示词
    作为 ``user_message`` 持久化到长期记忆,本检测用于在抽取/写入前过滤掉。
    """
    if not text:
        return False
    return "你正在执行一个已经到期的定时任务" in text or "定时任务 ID：" in text


def _strip_volatile_prefix(text: str) -> str:
    """从一段可能含污染前缀的文本里剥离系统注入的 volatile 前缀。

    扫描已知的污染特征,如果检测到、并且能定位到原始用户消息(以双换行分隔),
    返回去掉前缀的部分;否则保守返回原文。
    """
    if not text:
        return text
    if not any(marker in text for marker in _VOLATILE_PREFIX_STRIP_MARKERS):
        return text
    # 启发式：取最后一段没有污染特征的双换行后内容作为"真正的用户消息"。
    parts = text.split("\n\n")
    for idx in range(len(parts) - 1, -1, -1):
        candidate = parts[idx].strip()
        if not candidate:
            continue
        if any(marker in candidate for marker in _VOLATILE_PREFIX_STRIP_MARKERS):
            continue
        return candidate
    return text


def _extract_clean_user_message(user_message: str, task_frame: dict[str, object] | None) -> str:
    """优先取 task_frame.user_request 作为真用户输入,缺失时回退到剥离前缀。"""
    if isinstance(task_frame, dict):
        clean = task_frame.get("user_request")
        if isinstance(clean, str) and clean.strip():
            return clean.strip()
    return _strip_volatile_prefix(user_message).strip()


def capture_turn_memories(
    config: AppConfig,
    scope: MemoryScope,
    user_message: str,
    assistant_output: str,
    *,
    task_frame: dict[str, object] | None = None,
    memory_context_ids: list[str] | None = None,
    status: str = "completed",
    duration_ms: int = 0,
    source: str = "user",
) -> str | None:
    """把本轮对话写入长期记忆。

    v2 关键约束:
    - ``source != "user"`` 的 turn(scheduler / system)只写 L0 turn 行,**不**入 L1
      抽取队列、**不**做规则抽取(避免定时任务 payload 被当作长期事实)。
    - 写入前先用 ``_extract_clean_user_message`` 把 user_message 还原成"用户真正
      说的那句话",优先以 ``task_frame.user_request`` 为准;这样 L1 抽取看到的
      就不再是带 volatile 前缀的污染版本。
    - 如检测到内容仍像定时任务执行 payload,直接拒绝写入 L1。
    """
    if not config.memory.enabled or not config.memory.capture.enabled:
        return None

    clean_user_message = _extract_clean_user_message(user_message, task_frame)

    store = get_memory_store(config)
    turn = MemoryTurn(
        tenant_id=scope.tenant_id,
        user_id=scope.user_id,
        role_name=scope.role_name,
        session_id=scope.session_id,
        channel=scope.channel,
        user_message=clean_user_message,
        assistant_output=assistant_output,
        task_frame_json=json.dumps(task_frame or {}, ensure_ascii=False),
        memory_context_ids_json=json.dumps(memory_context_ids or [], ensure_ascii=False),
        status=status,
        duration_ms=duration_ms,
        source=source,
    )
    turn_id = store.add_turn(turn)

    # 非用户 turn(scheduler / system 下发):只留 L0 turn,不进 L1 抽取
    if source != "user":
        logger.info(
            "memory turn captured (no L1 extraction, source=%s) role=%s session_id=%s turn_id=%s",
            source,
            scope.role_name,
            scope.session_id,
            turn_id,
        )
        return turn_id

    # 内容形态像 scheduler payload 也跳过(防御已迁移到新通道前的回退路径污染)
    if _looks_like_scheduler_payload(clean_user_message):
        logger.warning(
            "memory L1 extraction skipped: user_message looks like scheduler payload role=%s session_id=%s turn_id=%s",
            scope.role_name,
            scope.session_id,
            turn_id,
        )
        return turn_id

    if len(clean_user_message.strip()) < config.memory.capture.min_turn_chars:
        return turn_id

    payload = {
        "turn_id": turn_id,
        "scope": scope.to_json_dict(),
        "user_message": clean_user_message,
        "assistant_output": assistant_output,
        "explicit_atom_ids": [],
    }
    store.enqueue_unique_job("extract_atoms_from_turn", payload, dedupe_key=f"extract:{turn_id}")

    if _looks_memorable(clean_user_message):
        guard = PrivacyGuard(
            allow_secret_memory=config.memory.privacy.allow_secret_memory,
            pii_redaction=config.memory.privacy.pii_redaction,
        )
        decision = guard.should_store(clean_user_message)
        if decision.action == "reject":
            logger.warning("memory candidate rejected role=%s session_id=%s reason=%s", scope.role_name, scope.session_id, decision.reason)
        if decision.action != "reject":
            if decision.action == "redact":
                logger.info("memory candidate redacted role=%s session_id=%s reason=%s", scope.role_name, scope.session_id, decision.reason)
            content = decision.text.strip()
            atom = MemoryAtom(
                memory_type="preference" if any(word in clean_user_message.lower() for word in ["偏好", "喜欢", "不喜欢", "以后", "要求", "prefer", "preference", "like", "dislike"]) else "project_context",
                content=content,
                scope=scope,
                subject="user",
                predicate="states",
                object=content,
                evidence_turn_ids=[turn_id],
                source_quote=content[:500],
                keywords=_keywords(content),
                confidence=0.82,
                stability=MemoryStability.STABLE if any(word in clean_user_message.lower() for word in ["以后", "偏好", "习惯", "要求", "prefer", "preference"]) else MemoryStability.SITUATIONAL,
                sensitivity=decision.sensitivity,
            )
            embedding = None
            if config.memory.embedding.enabled:
                embedding = EmbeddingProvider(config.memory.embedding).embed_sync(atom.content)
            decision_conflict = resolve_atom_conflict(
                store,
                atom,
                embedding_vector=embedding.vector if embedding and not embedding.degraded else None,
                embedding_model=config.memory.embedding.model if embedding and not embedding.degraded else None,
            )
            atom_id = decision_conflict.winner_id or atom.id
            if decision_conflict.action != "dedupe":
                atom_id = store.add_atom(atom)
                if decision_conflict.action == "supersede" and decision_conflict.loser_id:
                    store.mark_atom_superseded(decision_conflict.loser_id, atom_id, decision_conflict.conflict_group_id)
                    store.record_conflict(scope, decision_conflict.conflict_key, atom_id, decision_conflict.loser_id, decision_conflict.reason)
            if config.memory.embedding.enabled and decision_conflict.action != "dedupe":
                if embedding is None:
                    embedding = EmbeddingProvider(config.memory.embedding).embed_sync(atom.content)
                if embedding.degraded:
                    store.set_atom_embedding_status(atom_id, "failed")
                    logger.warning(
                        "memory atom embedding failed atom_id=%s role=%s session_id=%s reason=%s",
                        atom_id,
                        scope.role_name,
                        scope.session_id,
                        embedding.reason,
                    )
                else:
                    store.save_vector(atom_id, "L1", config.memory.embedding.model, embedding.vector)
                    logger.info(
                        "memory atom embedding stored atom_id=%s role=%s session_id=%s dimensions=%d",
                        atom_id,
                        scope.role_name,
                        scope.session_id,
                        len(embedding.vector),
                    )

    logger.info("memory turn captured role=%s session_id=%s turn_id=%s", scope.role_name, scope.session_id, turn_id)
    return turn_id
