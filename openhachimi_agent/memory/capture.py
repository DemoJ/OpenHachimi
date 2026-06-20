"""长期记忆捕获与抽取。"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.redaction import redact_text
from openhachimi_agent.memory.conflicts import resolve_atom_conflict
from openhachimi_agent.memory.embeddings import EmbeddingProvider
from openhachimi_agent.memory.models import MemoryAtom, MemoryScope, MemoryStability, MemoryTurn
from openhachimi_agent.memory.privacy import PrivacyGuard
from openhachimi_agent.memory.recall import get_memory_store

logger = logging.getLogger(__name__)


def _keywords(text: str) -> list[str]:
    words = []
    for part in "".join(char if char.isalnum() or "一" <= char <= "鿿" else " " for char in text).split():
        if len(part) >= 2:
            words.append(part[:32])
    return list(dict.fromkeys(words))[:12]


def _looks_memorable(user_message: str) -> bool:
    markers = ["记住", "以后", "偏好", "喜欢", "不喜欢", "习惯", "要求", "纠正", "不要", "项目", "背景", "决定", "remember", "prefer", "preference", "like", "dislike"]
    return any(marker in user_message for marker in markers)


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
) -> str | None:
    if not config.memory.enabled or not config.memory.capture.enabled:
        return None

    store = get_memory_store(config)
    turn = MemoryTurn(
        tenant_id=scope.tenant_id,
        user_id=scope.user_id,
        role_name=scope.role_name,
        session_id=scope.session_id,
        channel=scope.channel,
        user_message=user_message,
        assistant_output=assistant_output,
        task_frame_json=json.dumps(task_frame or {}, ensure_ascii=False),
        memory_context_ids_json=json.dumps(memory_context_ids or [], ensure_ascii=False),
        status=status,
        duration_ms=duration_ms,
    )
    turn_id = store.add_turn(turn)

    if len(user_message.strip()) < config.memory.capture.min_turn_chars:
        return turn_id

    payload = {
        "turn_id": turn_id,
        "scope": scope.to_json_dict(),
        "user_message": user_message,
        "assistant_output": assistant_output,
        "explicit_atom_ids": [],
    }
    store.enqueue_unique_job("extract_atoms_from_turn", payload, dedupe_key=f"extract:{turn_id}")

    if _looks_memorable(user_message):
        guard = PrivacyGuard(
            allow_secret_memory=config.memory.privacy.allow_secret_memory,
            pii_redaction=config.memory.privacy.pii_redaction,
        )
        decision = guard.should_store(user_message)
        if decision.action == "reject":
            logger.warning("memory candidate rejected role=%s session_id=%s reason=%s", scope.role_name, scope.session_id, decision.reason)
        if decision.action != "reject":
            if decision.action == "redact":
                logger.info("memory candidate redacted role=%s session_id=%s reason=%s", scope.role_name, scope.session_id, decision.reason)
            content = decision.text.strip()
            atom = MemoryAtom(
                memory_type="preference" if any(word in user_message.lower() for word in ["偏好", "喜欢", "不喜欢", "以后", "要求", "prefer", "preference", "like", "dislike"]) else "project_context",
                content=content,
                scope=scope,
                subject="user",
                predicate="states",
                object=content,
                evidence_turn_ids=[turn_id],
                source_quote=content[:500],
                keywords=_keywords(content),
                confidence=0.82,
                stability=MemoryStability.STABLE if any(word in user_message.lower() for word in ["以后", "偏好", "习惯", "要求", "prefer", "preference"]) else MemoryStability.SITUATIONAL,
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


def _serialize_window_for_rescue(messages: list[ModelMessage], *, max_chars: int = 8000) -> str:
    """把压缩丢弃的窗口序列化为可检索文本(脱敏)。"""
    lines: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in getattr(msg, "parts", None) or []:
                text = getattr(part, "content", None)
                if isinstance(text, str) and text.strip():
                    lines.append(redact_text(text.strip()))
                elif text is not None:
                    try:
                        lines.append(redact_text(json.dumps(text, ensure_ascii=False, default=str)))
                    except (TypeError, ValueError):
                        pass
        elif isinstance(msg, ModelResponse):
            for part in getattr(msg, "parts", None) or []:
                if isinstance(part, ToolCallPart):
                    args = part.args
                    args_text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str)
                    lines.append(redact_text(f"[{part.tool_name}] {args_text}"))
                else:
                    text = getattr(part, "content", "")
                    if isinstance(text, str) and text.strip():
                        lines.append(redact_text(text.strip()))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[已截断]"
    return text


def capture_compressed_window(
    config: AppConfig,
    scope: MemoryScope,
    messages: list[ModelMessage],
    window: list[ModelMessage],
) -> str | None:
    """压缩丢弃中间窗口前的抢救钩子:把待丢弃窗口存为可向量检索的 L1 atom。

    这样被压缩掉的工具调用细节仍可通过 :func:`recall_memories` 召回找回,
    形成"压缩丢旧 + 召回找旧"闭环。失败不影响压缩主流程。
    """
    if not config.context.rescue_to_memory or not window:
        return None
    if not config.memory.enabled:
        return None
    try:
        store = get_memory_store(config)
        content = _serialize_window_for_rescue(window)
        if len(content.strip()) < 50:
            return None
        atom = MemoryAtom(
            memory_type="conversation_context",
            content=content,
            scope=scope,
            subject="session",
            predicate="compressed",
            object="context window rescued before compression",
            source_quote=content[:500],
            keywords=_keywords(content),
            tags=["compressed", "rescued"],
            confidence=0.6,
            stability=MemoryStability.EPHEMERAL,
        )
        embedding = None
        if config.memory.embedding.enabled:
            embedding = EmbeddingProvider(config.memory.embedding).embed_sync(atom.content)
        decision = resolve_atom_conflict(
            store,
            atom,
            embedding_vector=embedding.vector if embedding and not embedding.degraded else None,
            embedding_model=config.memory.embedding.model if embedding and not embedding.degraded else None,
        )
        atom_id = decision.winner_id or atom.id
        if decision.action != "dedupe":
            atom_id = store.add_atom(atom)
        if config.memory.embedding.enabled and decision.action != "dedupe" and embedding and not embedding.degraded:
            store.save_vector(atom_id, "L1", config.memory.embedding.model, embedding.vector)
        logger.info(
            "compressed window rescued to memory role=%s session_id=%s atom_id=%s chars=%d",
            scope.role_name,
            scope.session_id,
            atom_id,
            len(content),
        )
        return atom_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture_compressed_window failed role=%s session_id=%s: %s", scope.role_name, scope.session_id, exc)
        return None
