"""阶段 3:结构化 LLM 摘要生成。

提供 :func:`build_summarizer`,返回符合 :data:`SummaryFn` 签名的同步摘要器。
摘要器用可配置辅助模型(留空时用主模型)通过 pydantic-ai ``run_sync`` 调用,
应在 ``asyncio.to_thread`` 中调用以避免阻塞事件循环。

摘要前对工具结果做程序化脱敏(复用 ``core/redaction.py``),优于纯 prompt 指令。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.context.compressor import SummaryFn
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.redaction import redact_text

logger = logging.getLogger(__name__)

# 单次摘要输入的最大字符数(防止把过大窗口喂给摘要模型)
_MAX_TURNS_CHARS = 120_000


def _serialize_turns(turns: list[ModelMessage]) -> str:
    """把待摘要的窗口序列化为可读文本,并对工具结果脱敏。"""
    lines: list[str] = []
    for idx, msg in enumerate(turns, start=1):
        if isinstance(msg, ModelRequest):
            for part in getattr(msg, "parts", None) or []:
                if hasattr(part, "content"):
                    text = _part_text(part.content)
                    if text:
                        lines.append(f"[轮{idx} 用户/工具返回] {redact_text(text)}")
        elif isinstance(msg, ModelResponse):
            for part in getattr(msg, "parts", None) or []:
                if isinstance(part, ToolCallPart):
                    args = part.args
                    args_text = args if isinstance(args, str) else _safe_json(args)
                    lines.append(f"[轮{idx} 工具调用 {part.tool_name}] {redact_text(args_text)}")
                else:
                    text = getattr(part, "content", "")
                    if isinstance(text, str) and text.strip():
                        lines.append(f"[轮{idx} 助手] {redact_text(text)}")
    text = "\n".join(lines)
    if len(text) > _MAX_TURNS_CHARS:
        text = text[:_MAX_TURNS_CHARS] + "\n...[已截断]"
    return text


def _part_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return _safe_json(content)
    return str(content)


def _safe_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def build_summarizer(config: AppConfig) -> SummaryFn:
    """构建同步 LLM 摘要器。

    用 ``config.context.summary`` 配置的辅助模型;留空时用主模型。
    返回的函数签名为 ``(turns, focus_topic, previous_summary) -> str | None``,
    通过 ``run_sync`` 调用,需在 ``asyncio.to_thread`` 中执行。
    """
    summary_cfg = config.context.summary
    model_name = summary_cfg.model or config.model_name
    base_url = summary_cfg.base_url or config.openai_base_url
    api_key = summary_cfg.api_key or config.openai_api_key

    if not api_key:
        logger.warning("摘要器无可用 API Key,将回退到确定性兜底摘要")
        return _noop_summarizer

    provider = OpenAIProvider(base_url=base_url or None, api_key=api_key)
    model = OpenAIChatModel(model_name, provider=provider)
    system_prompt = load_system_prompt("context/summary")
    agent = Agent(
        model,
        system_prompt=system_prompt,
        output_type=str,
        defer_model_check=True,
        retries=2,
    )

    def _summarize(
        turns: list[ModelMessage],
        focus_topic: str | None,
        previous_summary: str | None,
    ) -> str | None:
        from datetime import datetime

        try:
            current_date = datetime.now().strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            current_date = ""
        turns_text = _serialize_turns(turns)
        sections = [f"当前日期:{current_date}" if current_date else "", f"待压缩的对话轮次:\n{turns_text}"]
        if previous_summary:
            sections.append(f"上一份摘要(在此基础上增量更新):\n{previous_summary}")
        if focus_topic:
            sections.append(f"焦点主题(优先保留相关信息):{focus_topic}")
        user_message = "\n\n".join(s for s in sections if s)

        try:
            result = agent.run_sync(user_message, model_settings={"max_tokens": summary_cfg.max_tokens})
            output = result.output
            if isinstance(output, str) and output.strip():
                return output.strip()
            logger.warning("摘要器返回空输出")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("摘要器 LLM 调用失败:%s", exc)
            raise

    return _summarize


def _noop_summarizer(
    turns: list[ModelMessage],
    focus_topic: str | None,
    previous_summary: str | None,
) -> str | None:
    """无可用模型时的空摘要器,返回 None 触发兜底。"""
    return None


__all__ = ["build_summarizer"]
