"""长期记忆召回流程。"""

from __future__ import annotations

import atexit
import logging
from datetime import datetime
from pathlib import Path

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.embeddings import EmbeddingProvider
from openhachimi_agent.memory.formatting import format_memory_context
from openhachimi_agent.memory.models import CORE_MEMORY_TYPES, MemoryContext, MemoryScope, MemorySearchResult, MemoryType
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


def close_memory_stores() -> None:
    """关闭并清空进程级 store 缓存。

    幂等：供 ``atexit`` 钩子与测试夹具共用。生产侧 ``http`` / ``cli`` 长期持有
    store 实例，本函数确保进程退出时显式释放 SQLite 连接，避免依赖 ``__del__``
    的不确定回收时机触发 ``ResourceWarning``。
    """
    for store in _STORE_CACHE.values():
        try:
            store.close()
        except Exception:
            logger.debug("memory store close failed during cleanup", exc_info=True)
    _STORE_CACHE.clear()


atexit.register(close_memory_stores)


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


# 不进自动召回的类型(L1 原子和 L2 主题块都排除):
# - scheduler_payload:定时任务下发的执行内容,不是用户事实;
# - task_reference:历史任务摘要,与当前对话大多无关,需要时由模型调 search_memory 查。
#   旧实现靠一套针对 lxh.io 写死的域名 / 动作词规则判断"是否相关",已删除。
_EXCLUDED_AUTO_RECALL_TYPES = {"scheduler_payload", "task_reference"}


# 任务意图到记忆类型的映射:不同任务类型召回不同维度的记忆
INTENT_TYPE_MAP: dict[str, list[str]] = {
    "coding": [
        MemoryType.PROJECT_FACT.value,
        MemoryType.PROJECT_DECISION.value,
        MemoryType.USER_CONSTRAINT.value,
        MemoryType.WORKFLOW.value,
    ],
    "chat": [
        MemoryType.USER_TRAIT.value,
        MemoryType.USER_PREFERENCE.value,
        MemoryType.USER_CONSTRAINT.value,
    ],
    "research": [
        MemoryType.TASK_REFERENCE.value,
        MemoryType.PROJECT_FACT.value,
        MemoryType.USER_PREFERENCE.value,
    ],
    "planning": [
        MemoryType.PROJECT_FACT.value,
        MemoryType.PROJECT_DECISION.value,
        MemoryType.WORKFLOW.value,
        MemoryType.USER_CONSTRAINT.value,
    ],
    "general": [
        MemoryType.USER_PREFERENCE.value,
        MemoryType.USER_CONSTRAINT.value,
        MemoryType.PROJECT_FACT.value,
    ],
}


def infer_task_intent(query: str) -> str:
    """从查询文本推断任务意图类型。

    基于关键词启发式分类,不追求精确,只做粗粒度过滤:
    - coding: 代码/编程/调试/报错相关
    - research: 调研/搜索/分析/深挖相关
    - planning: 计划/设计/架构/方案相关
    - chat: 闲聊/问答/建议相关
    - general: 默认
    """
    text = query.lower()
    coding_terms = ("代码", "编程", "debug", "报错", "error", "bug", "fix", "修复", "实现", "implement", "function", "class", "import", "python", "javascript", "typescript", "react", "vue", "数据库", "sql", "api", "接口", "服务器", "部署", "deploy")
    research_terms = ("调研", "搜索", "查找", "分析", "深挖", "挖掘", "research", "search", "find", "analyze", " investigate", "study", "了解")
    planning_terms = ("计划", "规划", "设计", "架构", "方案", "plan", "design", "architecture", "schema", "roadmap", "路线图", "流程", "workflow")
    chat_terms = ("聊天", "聊聊", "怎么看", "你觉得", "建议", "意见", "看法", "chat", "talk", "discuss", "opinion", "advice")

    scores = {"coding": 0, "research": 0, "planning": 0, "chat": 0}
    for term in coding_terms:
        if term in text:
            scores["coding"] += 1
    for term in research_terms:
        if term in text:
            scores["research"] += 1
    for term in planning_terms:
        if term in text:
            scores["planning"] += 1
    for term in chat_terms:
        if term in text:
            scores["chat"] += 1

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "general"
    return best


# 与当前意图匹配的记忆类型的加权系数。
_INTENT_BOOST = 1.25


def apply_intent_boost(results: list[MemorySearchResult], intent: str) -> list[MemorySearchResult]:
    """按任务意图给匹配的记忆类型加权,不丢弃任何记忆。

    意图是关键词猜出来的,不可靠:硬过滤会在"修 bug"时丢掉全部用户偏好,
    还会让 L3 画像(user_profile)和未登记类型永远召回不到。所以这里只调整
    排序,最终条数仍由 select_by_level_budget 控制。
    """
    preferred = set(INTENT_TYPE_MAP.get(intent, INTENT_TYPE_MAP["general"]))
    for item in results:
        if item.memory_type in preferred:
            item.score *= _INTENT_BOOST
    results.sort(key=lambda item: item.score, reverse=True)
    return results


# 两条记忆的字符二元组 Jaccard 相似度达到该值才视为重复。取值偏高是有意的:
# "用中文回答" 与 "不要用中文回答" 意思相反,不能被合并。
_DEDUPE_SIMILARITY = 0.85


def _dedupe_text(text: str) -> str:
    """去掉空白和标点并转小写,只保留字母数字和汉字。"""
    return "".join(char for char in text.lower() if char.isalnum())


def _char_bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text}
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _is_near_duplicate(left: str, right: str) -> bool:
    if left == right:
        return True
    left_grams, right_grams = _char_bigrams(left), _char_bigrams(right)
    union = left_grams | right_grams
    if not union:
        return False
    return len(left_grams & right_grams) / len(union) >= _DEDUPE_SIMILARITY


def dedupe_by_content(results: list[MemorySearchResult]) -> list[MemorySearchResult]:
    """按内容去重:内容相同或几乎相同的记忆只保留分数最高的一条。

    旧实现按 subject+predicate 分组,会把"用中文回答""回答要简洁"这类
    主谓相同但内容不同的偏好合并成一条。
    """
    kept: list[MemorySearchResult] = []
    kept_texts: list[str] = []
    for item in sorted(results, key=lambda result: result.score, reverse=True):
        text = _dedupe_text(item.content)
        if any(_is_near_duplicate(text, seen) for seen in kept_texts):
            continue
        kept.append(item)
        kept_texts.append(text)
    return kept


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
        if item.level == "L1" and item.memory_type not in _EXCLUDED_AUTO_RECALL_TYPES
    ][:final_l1_top_k]
    l2 = [
        item for item in results
        if item.level == "L2" and item.memory_type not in _EXCLUDED_AUTO_RECALL_TYPES
    ][:final_l2_top_k]
    selected.extend(l1)
    selected.extend(l2)
    if include_l3_profile:
        selected.extend([item for item in results if item.level == "L3"][:1])
    selected.sort(key=lambda item: item.score, reverse=True)
    return selected


# 每轮常驻注入的记忆条数上限(按置信度取前 N 条)。
_CORE_MEMORY_LIMIT = 12
# 检索兜底来源:FTS 没命中时退回"最近写入的几条",与当前问题无关,不自动注入。
_UNMATCHED_SOURCES = {"recent"}


def _retrieve_relevant(
    config: AppConfig, store: MemoryStore, scope: MemoryScope, query: str
) -> tuple[list[MemorySearchResult], bool, str]:
    """按当前消息检索相关记忆(BM25 + 向量 + RRF 融合)。返回 (结果, 是否降级, 降级原因)。"""
    bm25_results = store.search(scope, query, limit=config.memory.recall.bm25_top_k, touch_results=False)
    bm25_results = [item for item in bm25_results if item.source not in _UNMATCHED_SOURCES]
    vector_results: list[MemorySearchResult] = []
    degraded, reason = False, ""
    if config.memory.embedding.enabled:
        embedding = EmbeddingProvider(config.memory.embedding).embed_sync(query)
        if embedding.degraded:
            degraded, reason = True, embedding.reason
            logger.warning("memory vector recall skipped role=%s session_id=%s reason=%s", scope.role_name, scope.session_id, embedding.reason)
        else:
            vector_results = store.vector_search(scope, embedding.vector, limit=config.memory.recall.vector_top_k, model=config.memory.embedding.model, levels=("L1", "L2"))
    fused = fuse_results([bm25_results, vector_results], rrf_k=config.memory.recall.rrf_k)
    # 任务意图感知:匹配当前意图的记忆类型加权,不丢弃
    fused = apply_intent_boost(fused, infer_task_intent(query))
    # 内容去重:避免同一偏好被多条记忆重复表达
    fused = dedupe_by_content(fused)
    selected = select_by_level_budget(
        fused,
        final_l1_top_k=config.memory.recall.final_l1_top_k,
        final_l2_top_k=config.memory.recall.final_l2_top_k,
        include_l3_profile=config.memory.recall.include_l3_profile,
    )
    return selected, degraded, reason


def _drop_covered_by_core(retrieved: list[MemorySearchResult], core: list[MemorySearchResult]) -> list[MemorySearchResult]:
    """去掉已在常驻记忆里出现(同 id 或内容几乎相同)的检索结果。"""
    core_ids = {item.id for item in core}
    core_texts = [_dedupe_text(item.content) for item in core]
    return [
        item for item in retrieved
        if item.id not in core_ids
        and not any(_is_near_duplicate(_dedupe_text(item.content), text) for text in core_texts)
    ]


def recall_memories(config: AppConfig, scope: MemoryScope, query: str) -> MemoryContext:
    """本轮要放进系统指令的记忆 = 常驻记忆 + 与当前消息相关的检索结果。

    常驻层:用户偏好 / 约束 / 身份 / 工作方式,每轮都带上,不依赖检索命中——以前"用中文回答"
    这类偏好只有在当前消息碰巧检索到时才生效,表现为时有时无。
    检索层:其余记忆只注入真正命中的结果;没注入的由模型需要时调 search_memory 查。
    只有检索命中才刷新访问时间;常驻记忆每轮都注入,刷新它说明不了"用到了"。
    """
    if not config.memory.enabled:
        return MemoryContext(scope=scope, query=query)
    try:
        store = get_memory_store(config)
        core = store.list_core_atoms(scope, CORE_MEMORY_TYPES, limit=_CORE_MEMORY_LIMIT)
        retrieved: list[MemorySearchResult] = []
        degraded, reason = False, ""
        if query.strip():
            retrieved, degraded, reason = _retrieve_relevant(config, store, scope, query)
            retrieved = _drop_covered_by_core(retrieved, core)
            store.touch([item.id for item in retrieved])
        logger.info(
            "memory recall completed role=%s session_id=%s core=%d retrieved=%d degraded=%s sources=%s",
            scope.role_name,
            scope.session_id,
            len(core),
            len(retrieved),
            str(degraded).lower(),
            sorted({item.source for item in retrieved}),
        )
        return MemoryContext(scope=scope, query=query, results=[*core, *retrieved], degraded=degraded, reason=reason)
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
