"""上下文压缩。

`AgentService` 持有 `session_store` 与 `_context_compressors` 缓存,这里提供接收
`service` 整体作参数的纯函数,负责手动压缩会话上下文、构建/缓存会话级压缩器。
`AgentService.compress_session` / `_get_context_compressor` 退化为薄壳。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openhachimi_agent.transport.api_models import ChatResponse


logger = logging.getLogger(__name__)


async def compress_session(
    service,
    role: str,
    session_id: str,
    focus_topic: str = "",
    latest_scope: str | None = None,
) -> ChatResponse:
    """手动压缩指定会话的上下文历史(可带焦点主题)。

    append-only 语义下压缩只记元数据(``session_compressions``),绝不删原始消息。
    与 turn.py 的 ``run_turn`` 一样套住 per-session asyncio.Lock —— 旧实现
    没套,理论上一次手动 /compress 可能在 turn 写入 session 的瞬间插队读旧
    history 然后用陈旧值覆盖回去。SQLite 端虽有 ``BEGIN IMMEDIATE`` 兜底,
    应用层这把锁还是要拿,belt-and-suspenders。
    """
    role = service._normalize_role(role)
    actual_session_id, history = service.session_store.load_messages(role, session_id, latest_scope)
    if not history:
        return ChatResponse(output="当前会话无历史可压缩。", role=role, session_id=actual_session_id)
    compressor = service._get_context_compressor(actual_session_id)
    if compressor is None:
        return ChatResponse(output="上下文压缩未启用。", role=role, session_id=actual_session_id)
    if not compressor.has_content_to_compress(history):
        return ChatResponse(output="当前对话历史较短,暂无需压缩。", role=role, session_id=actual_session_id)
    focus = focus_topic.strip() or None
    before = len(history)
    lock = service._get_session_lock(actual_session_id)
    async with lock:
        try:
            # 压缩作用于完整原始序列(turn_index 升序),边界内存下标 == turn_index,映射零误差。
            result = await asyncio.to_thread(
                compressor.compress,
                history,
                focus_topic=focus,
                force=True,
            )
        except Exception as exc:
            logger.warning("manual compress failed role=%s session_id=%s: %s", role, actual_session_id, exc)
            return ChatResponse(output=f"压缩失败:{exc.__class__.__name__}", role=role, session_id=actual_session_id)
        if not result.dropped:
            return ChatResponse(
                output=f"未产生压缩(可能已无可压缩的中间窗口)。历史共 {before} 条消息。",
                role=role,
                session_id=actual_session_id,
            )
        comp_id = await asyncio.to_thread(
            service.session_store.record_compression,
            role,
            actual_session_id,
            result.head_end_idx,
            result.tail_start_idx,
            result.summary,
            total_len=len(history),
        )
    savings = compressor._last_compression_savings_pct  # noqa: SLF001
    focus_hint = f"(焦点:{focus})" if focus else ""
    dropped = result.tail_start_idx - result.head_end_idx - 1
    return ChatResponse(
        output=f"已压缩上下文{focus_hint}:折叠 {dropped} 条中间消息(第 {compressor.compression_count} 次压缩 / compression_id={comp_id},约省 {savings:.0f}%)。原始消息仍可在历史中展开查看。",
        role=role,
        session_id=actual_session_id,
    )


def get_context_compressor(service, session_id: str) -> Any:
    """获取或构建会话级上下文压缩器(含 LLM 摘要器)。"""
    cached = service._context_compressors.get(session_id)
    if cached is not None:
        return cached
    cfg = service.config.context
    if not cfg.enabled:
        return None
    from openhachimi_agent.context.compressor import ContextCompressor
    from openhachimi_agent.context.summary import build_summarizer

    summarizer = build_summarizer(service.config)

    compressor = ContextCompressor(
        threshold_percent=cfg.threshold_percent,
        hard_ceiling_percent=cfg.hard_ceiling_percent,
        protect_first_n=cfg.protect_first_n,
        protect_last_n=cfg.protect_last_n,
        tail_token_budget=cfg.tail_token_budget,
        anti_thrash=cfg.anti_thrash,
        min_savings_pct=cfg.min_savings_pct,
        # context_length 配置单位为 K,这里换算成 token(128K = 128000)传给压缩引擎
        context_length=cfg.context_length * 1000 if cfg.context_length else 0,
        abort_on_summary_failure=cfg.summary.abort_on_failure,
        summarizer=summarizer,
    )
    # 让 token 计数用与实际模型匹配的 tiktoken encoding(gpt-4o 系 -> o200k_base,
    # gpt-4 系 -> cl100k_base;未知第三方模型名回退默认),提升压缩预检/边界精度。
    from openhachimi_agent.context.token_estimate import set_model_for_token_estimate
    set_model_for_token_estimate(service.config.model_name)
    service._context_compressors[session_id] = compressor
    return compressor
