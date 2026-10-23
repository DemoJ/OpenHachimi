"""长期记忆捕获与抽取。"""

from __future__ import annotations

import json
import logging
from typing import Any

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.conflicts import resolve_atom_conflict
from openhachimi_agent.memory.embeddings import EmbeddingProvider
from openhachimi_agent.memory.models import MemoryAtom, MemoryScope, MemoryTurn
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


# 同步快速通道只认"明确要求记住"的说法,让"记住……"这类话本轮结束就能被召回。
# 其余消息一律交给后台 LLM 抽取判断。旧实现只要出现"项目 / 不要 / 喜欢 / like"
# 就把整句原话按 0.82 置信度存成长期偏好或约束,大量一次性任务指令被当成长期记忆。
_EXPLICIT_MEMORY_MARKERS = (
    "记住", "记得", "以后都", "以后请", "以后一律", "今后", "remember", "from now on",
)
# 含"记住"但后面跟的是本次任务的执行约束,不是要长期记住的偏好。
_TASK_CONSTRAINT_PATTERNS = (
    "记住不要", "记住必须", "记住一定", "记住务必", "记住直到",
    "记住在", "记住当", "记住如果", "记住对于",
)


def _is_question_like(text: str) -> bool:
    return text.strip().endswith(("？", "?"))


def _is_explicit_memory_request(text: str) -> bool:
    """用户是否明确要求记住某件事(而不是在提问或给本次任务加约束)。"""
    lowered = text.lower()
    if not any(marker in lowered for marker in _EXPLICIT_MEMORY_MARKERS):
        return False
    if any(pattern in text for pattern in _TASK_CONSTRAINT_PATTERNS):
        return False
    return not _is_question_like(text)



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


# runtime 注入的附件元数据块标志。message_with_attachments 会把"用户原话 +
# 附件元数据块(+视觉前缀)"用双换行拼接后挂到 state.inputs.effective_message,
# 此块作为 user_message 进入记忆捕获时会带着"不要"等词错误命中 _is_memorable_turn
# 的正向闸门,导致图片交互内容被当作长期事实抽取,故在清洗时剥离,只保留用户原话。
_ATTACHMENT_BLOCK_MARKER = "用户同时发送了以下附件："


def _strip_attachment_block(text: str) -> str:
    """剥离 runtime 注入的附件元数据块,还原用户真正说的那句话。

    附件块用双换行拼在用户原话之后,取其前的内容即可;附件块内部以单换行
    组织(无双换行),``partition`` 不会误切。不含附件块时原样返回。
    """
    if not text or _ATTACHMENT_BLOCK_MARKER not in text:
        return text
    head, _, _ = text.partition(_ATTACHMENT_BLOCK_MARKER)
    return head


def _extract_clean_user_message(user_message: str) -> str:
    """剥离系统注入的前缀和附件元数据块,还原用户真正说的那句话。"""
    cleaned = _strip_volatile_prefix(user_message)
    cleaned = _strip_attachment_block(cleaned)
    return cleaned.strip()


def _l1_extraction_allowed(config: AppConfig, scope: MemoryScope, clean_user_message: str, source: str, turn_id: str) -> bool:
    """这一轮是否进入 L1 抽取(L0 turn 记录无论如何都已写入)。"""
    # 非用户 turn(scheduler / system 下发):只留 L0 turn,不进 L1 抽取
    if source != "user":
        logger.info(
            "memory turn captured (no L1 extraction, source=%s) role=%s session_id=%s turn_id=%s",
            source,
            scope.role_name,
            scope.session_id,
            turn_id,
        )
        return False
    # 内容形态像 scheduler payload 也跳过(防御已迁移到新通道前的回退路径污染)
    if _looks_like_scheduler_payload(clean_user_message):
        logger.warning(
            "memory L1 extraction skipped: user_message looks like scheduler payload role=%s session_id=%s turn_id=%s",
            scope.role_name,
            scope.session_id,
            turn_id,
        )
        return False
    return len(clean_user_message.strip()) >= config.memory.capture.min_turn_chars


def _enqueue_extraction(
    store: Any,
    scope: MemoryScope,
    turn_id: str,
    clean_user_message: str,
    assistant_output: str,
    recent_turns: list[dict[str, str]],
) -> None:
    """所有用户消息都进后台 LLM 抽取,由 LLM 判断是否值得长期记住。"""
    store.enqueue_unique_job(
        "extract_atoms_from_turn",
        {
            "turn_id": turn_id,
            "scope": scope.to_json_dict(),
            "user_message": clean_user_message,
            "assistant_output": assistant_output,
            "recent_turns": recent_turns,
            "explicit_atom_ids": [],
        },
        dedupe_key=f"extract:{turn_id}",
    )


def capture_turn_memories(
    config: AppConfig,
    scope: MemoryScope,
    user_message: str,
    assistant_output: str,
    *,
    memory_context_ids: list[str] | None = None,
    status: str = "completed",
    duration_ms: int = 0,
    source: str = "user",
    skip_explicit: bool = False,
) -> str | None:
    """把本轮对话写入长期记忆。

    v2 关键约束:
    - ``source != "user"`` 的 turn(scheduler / system)只写 L0 turn 行,**不**入 L1
      抽取队列、**不**做规则抽取(避免定时任务 payload 被当作长期事实)。
    - 写入前先用 ``_extract_clean_user_message`` 把 user_message 还原成"用户真正
      说的那句话";这样 L1 抽取看到的就不再是带 volatile 前缀的污染版本。
    - 如检测到内容仍像定时任务执行 payload,直接拒绝写入 L1。
    """
    if not config.memory.enabled or not config.memory.capture.enabled:
        return None

    clean_user_message = _extract_clean_user_message(user_message)

    store = get_memory_store(config)
    # 写入本轮之前取前几轮,给后台抽取当上下文
    recent_turns = store.list_recent_user_turns(scope.session_id, limit=_RECENT_TURNS_FOR_EXTRACTION)
    turn_id = store.add_turn(MemoryTurn(
        tenant_id=scope.tenant_id,
        user_id=scope.user_id,
        role_name=scope.role_name,
        session_id=scope.session_id,
        channel=scope.channel,
        user_message=clean_user_message,
        assistant_output=assistant_output,
        memory_context_ids_json=json.dumps(memory_context_ids or [], ensure_ascii=False),
        status=status,
        duration_ms=duration_ms,
        source=source,
    ))
    if not _l1_extraction_allowed(config, scope, clean_user_message, source, turn_id):
        return turn_id
    _enqueue_extraction(store, scope, turn_id, clean_user_message, assistant_output, recent_turns)
    if not skip_explicit and _is_explicit_memory_request(clean_user_message):
        stored = _capture_explicit_memories(config, store, scope, clean_user_message, turn_id)
        logger.info("memory explicit request captured atoms=%d role=%s session_id=%s turn_id=%s", stored, scope.role_name, scope.session_id, turn_id)
    logger.info("memory turn captured role=%s session_id=%s turn_id=%s", scope.role_name, scope.session_id, turn_id)
    return turn_id


# session_state 里的标记:模型本轮调用过 remember。由 remember 工具写入,本轮记忆捕获时取出,
# 每轮开头(main_agent._reset_turn_gates)清掉。
REMEMBERED_THIS_TURN_KEY = "_remembered_this_turn"

# 后台抽取时附带的同会话前几轮对话数。
_RECENT_TURNS_FOR_EXTRACTION = 2

# 用户明确要求记住的内容,置信度高于普通规则抽取(0.74)。
_EXPLICIT_MEMORY_CONFIDENCE = 0.85


def _capture_explicit_memories(config: AppConfig, store: Any, scope: MemoryScope, text: str, turn_id: str) -> int:
    """用户明确要求记住时,按子句拆开同步写入,本轮结束即可被召回。返回写入条数。

    只存拆出来的偏好 / 约束子句,不存整句原话。后台 LLM 抽取随后还会再看这句话,
    同内容的子句会被冲突检查去重。
    """
    # extraction 依赖本模块的工具函数,放到函数内 import 避免循环引用
    from openhachimi_agent.memory.extraction import _extract_with_rules

    guard = PrivacyGuard(
        allow_secret_memory=config.memory.privacy.allow_secret_memory,
        pii_redaction=config.memory.privacy.pii_redaction,
    )
    decision = guard.should_store(text)
    if decision.action == "reject":
        logger.warning("memory candidate rejected role=%s session_id=%s reason=%s", scope.role_name, scope.session_id, decision.reason)
        return 0
    stored = 0
    for extracted in _extract_with_rules(decision.text.strip(), guard).memories:
        atom = MemoryAtom(
            memory_type=extracted.memory_type,
            content=extracted.content,
            scope=scope,
            subject=extracted.subject,
            predicate=extracted.predicate,
            object=extracted.object,
            evidence_turn_ids=[turn_id],
            source_quote=extracted.source_quote,
            keywords=extracted.keywords,
            tags=extracted.tags,
            confidence=_EXPLICIT_MEMORY_CONFIDENCE,
            stability=extracted.stability,
            sensitivity=extracted.sensitivity,
            valid_until=extracted.valid_until,
            decay_at=extracted.decay_at,
        )
        if _store_atom_with_embedding(config, store, atom):
            stored += 1
    return stored


def _store_atom_with_embedding(config: AppConfig, store: Any, atom: MemoryAtom) -> bool:
    """冲突检查后写入 atom,并尽量同步写入向量。被判为重复时不写,返回 False。"""
    embedding = None
    if config.memory.embedding.enabled:
        embedding = EmbeddingProvider(config.memory.embedding).embed_sync(atom.content)
    vector_ok = embedding is not None and not embedding.degraded
    conflict = resolve_atom_conflict(
        store,
        atom,
        embedding_vector=embedding.vector if vector_ok else None,
        embedding_model=config.memory.embedding.model if vector_ok else None,
    )
    if conflict.action == "dedupe":
        return False
    atom_id = store.add_atom(atom)
    if vector_ok:
        store.save_vector(atom_id, "L1", config.memory.embedding.model, embedding.vector)
    elif embedding is not None:
        store.set_atom_embedding_status(atom_id, "failed")
        logger.warning("memory atom embedding failed atom_id=%s reason=%s", atom_id, embedding.reason)
    return True
