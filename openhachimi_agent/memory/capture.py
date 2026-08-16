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


# LLM 抽取作业入队:所有用户消息都进 LLM 抽取,由 LLM 判断是否为可记忆内容。
# 移除关键词过滤闸门——LLM 比规则更能理解语义,避免"记住不要停"这类任务指令
# 被误判为记忆意图,也避免"帮我研究一下"这类隐性任务指令被漏掉。
# 隐私保护由 PrivacyGuard 在抽取后统一处理,不依赖前置关键词过滤。
# 任务型指令特征:这类消息虽然可能含"记住"等词,但本质是一次性任务指令,
# 而非需要长期记忆的偏好或事实。识别后用于规则提取原子化内容,不存原文。
_TASK_COMMAND_MARKERS = (
    "深挖", "挖掘", "调研", "搜索", "查找", "分析", "爬取", "抓取", "采集",
    "必须", "一定", "务必", "记住不要", "不要还", "直到", "挖到", "挖无可挖",
    "所有事情", "任何事情", "所有信息", "剩余信息", "相关信息",
    "dig", "research", "search", "find", "analyze", "scrape", "crawl",
    "must", "until", "exhaust", "everything about",
)
# 复合任务指令:含"记住"但后面紧跟的是任务执行约束而非记忆意图
_TASK_CONSTRAINT_PATTERNS = (
    "记住不要", "记住必须", "记住一定", "记住务必", "记住直到",
    "记住在", "记住当", "记住如果", "记住对于",
)


def _is_question_like(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith(("？", "?")):
        return True
    return False


def _is_task_command(text: str) -> bool:
    """判断文本是否为任务型指令而非需要记忆的事实/偏好。

    保留用于规则提取原子信息,不再作为 LLM 抽取的过滤闸门。
    """
    lowered = text.lower()
    task_marker_count = sum(1 for marker in _TASK_COMMAND_MARKERS if marker in text or marker in lowered)
    if task_marker_count >= 2:
        return True
    if any(pattern in text for pattern in _TASK_CONSTRAINT_PATTERNS):
        return True
    has_command_verb = any(marker in text for marker in ("深挖", "挖掘", "调研", "搜索", "查找", "分析", "dig", "research", "search", "analyze"))
    has_target = any(char in text for char in (".", "。", "io", "com", "cn", "网站", "博主", "作者", "项目"))
    if len(text) > 50 and has_command_verb and has_target:
        return True
    return False


def _extract_atomic_from_task_command(text: str) -> str | None:
    """从任务型指令中提取原子化的关键信息,而非保存整段原文。

    例如: "深挖关于lxh.io这个网站的任何事情,以及深挖这个网站背后的博主"
    提取: "用户曾要求调研 lxh.io 网站及其博主相关信息"

    如果无法提取有意义的原子信息,返回 None。
    """
    import re
    # 提取目标对象(域名/网站/人名等)
    domains = re.findall(r'[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}', text)
    # 提取动作词
    actions = []
    for action in ("深挖", "挖掘", "调研", "搜索", "查找", "分析", "了解"):
        if action in text:
            actions.append(action)
    if not domains and not actions:
        return None
    parts = []
    if domains:
        parts.append(f"目标: {', '.join(domains[:3])}")
    if actions:
        parts.append(f"动作: {', '.join(dict.fromkeys(actions))}")
    return f"[历史任务] {' | '.join(parts)}"





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


def _extract_clean_user_message(user_message: str, task_frame: dict[str, object] | None) -> str:
    """优先取 task_frame.user_request 作为真用户输入,缺失时回退到剥离前缀。"""
    if isinstance(task_frame, dict):
        clean = task_frame.get("user_request")
        if isinstance(clean, str) and clean.strip():
            return clean.strip()
    cleaned = _strip_volatile_prefix(user_message)
    cleaned = _strip_attachment_block(cleaned)
    return cleaned.strip()


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

    # 任务型指令特殊处理:提取原子化关键信息,而非保存整段原文
    # 所有用户消息都进 LLM 抽取,由 LLM 判断是否为可记忆内容
    task_command_atomic = None
    if _is_task_command(clean_user_message):
        task_command_atomic = _extract_atomic_from_task_command(clean_user_message)
        if task_command_atomic:
            logger.info(
                "memory task command detected, storing atomic summary role=%s session_id=%s turn_id=%s",
                scope.role_name,
                scope.session_id,
                turn_id,
            )

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
            # 任务型指令:用原子化摘要替代原文,避免整段命令入库
            if task_command_atomic:
                content = task_command_atomic
                logger.info("memory atom content replaced with atomic summary role=%s session_id=%s", scope.role_name, scope.session_id)
            atom = MemoryAtom(
                memory_type="task_reference" if task_command_atomic else ("user_preference" if any(word in clean_user_message.lower() for word in ["偏好", "喜欢", "不喜欢", "以后", "要求", "prefer", "preference", "like", "dislike"]) else "user_constraint"),
                content=content,
                scope=scope,
                subject="user",
                predicate="states",
                object=content,
                evidence_turn_ids=[turn_id],
                source_quote=content[:500],
                keywords=_keywords(content),
                confidence=0.65 if task_command_atomic else 0.82,
                stability=MemoryStability.EPHEMERAL if task_command_atomic else (MemoryStability.STABLE if any(word in clean_user_message.lower() for word in ["以后", "偏好", "习惯", "要求", "prefer", "preference"]) else MemoryStability.SITUATIONAL),
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
