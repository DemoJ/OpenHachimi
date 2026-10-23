"""长期记忆 LLM 调用封装。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.core.config import AppConfig

logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=BaseModel)
# 单次后台记忆 LLM 调用的上限(秒)。超时按失败处理,由调用方降级(规则抽取 / 向量规则)。
_MEMORY_LLM_TIMEOUT_SECONDS = 120


class MemoryLLMItem(BaseModel):
    """Structured memory item returned by the extraction agent."""

    memory_type: str = "fact"
    content: str = ""
    subject: str = "user"
    predicate: str = "states"
    object: str = ""
    keywords: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    confidence: float = 0.7
    stability: str = "situational"
    source_quote: str = ""


class MemoryExtractionOutput(BaseModel):
    """Structured output for long-term memory extraction."""

    memories: list[MemoryLLMItem] = Field(default_factory=list)


class MemorySummaryOutput(BaseModel):
    """Structured output for long-term memory summarization."""

    summary: str = ""


class MemoryUpdateDecision(BaseModel):
    """新记忆对旧记忆的处理决定(ADD / UPDATE / DELETE / NONE)。"""

    index: int
    event: str = "ADD"
    target_id: str = ""
    content: str = ""


class MemoryUpdateOutput(BaseModel):
    """Structured output for deciding how new memories update existing ones."""

    decisions: list[MemoryUpdateDecision] = Field(default_factory=list)


def _memory_llm_available(config: AppConfig | None) -> bool:
    return bool(
        config
        and config.openai_api_key
        and config.openai_base_url
        and config.openai_base_url.strip()
    )


def _build_memory_model(config: AppConfig) -> OpenAIChatModel:
    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )
    return OpenAIChatModel(config.model_name, provider=provider)


def run_memory_extraction(
    config: AppConfig | None,
    *,
    system_prompt: str,
    payload: dict[str, Any],
) -> MemoryExtractionOutput | None:
    """Run structured long-term memory extraction through pydantic_ai.

    本函数是**同步**接口,调用方必须把它放到**没有运行中事件循环**的工作
    线程里执行(经 ``asyncio.to_thread``)——``MemoryScheduler.handle_job`` 即
    如此调用。切勿在主事件循环里直接同步调用,否则下面的 ``asyncio.run`` 会抛
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``,
    而 ``agent.run(...)`` 此时已被求值为协程对象、尚未被 await,协程随之泄漏,
    触发 ``coroutine 'AbstractAgent.run' was never awaited`` 警告。

    用 ``asyncio.run`` 而非 ``agent.run_sync``:后者内部用
    ``get_event_loop().run_until_complete(...)``,在工作线程里会创建并 ``set``
    一个**不关闭**的事件循环,跨 ``to_thread`` 调用残留,导致退出时
    Windows Proactor pipe transport 未清理(``OverlappedFuture 句柄无效``)。
    ``asyncio.run`` 每次创建并**关闭**临时循环,彻底清理 transport,杜绝泄漏。
    """
    return _run_structured(config, system_prompt=system_prompt, payload=payload, output_type=MemoryExtractionOutput, kind="extraction")


def run_memory_summary(
    config: AppConfig | None,
    *,
    system_prompt: str,
    payload: dict[str, Any],
) -> MemorySummaryOutput | None:
    """Run structured long-term memory summarization through pydantic_ai."""
    return _run_structured(config, system_prompt=system_prompt, payload=payload, output_type=MemorySummaryOutput, kind="summary")


def run_memory_update(
    config: AppConfig | None,
    *,
    system_prompt: str,
    payload: dict[str, Any],
) -> MemoryUpdateOutput | None:
    """让 LLM 决定新记忆对旧记忆做 ADD / UPDATE / DELETE / NONE。同步接口,调用约束同 run_memory_extraction。"""
    return _run_structured(config, system_prompt=system_prompt, payload=payload, output_type=MemoryUpdateOutput, kind="update")


def _run_structured(
    config: AppConfig | None,
    *,
    system_prompt: str,
    payload: dict[str, Any],
    output_type: type[OutputT],
    kind: str,
) -> OutputT | None:
    """三个记忆 LLM 调用的公共实现。LLM 未配置或调用失败时返回 None,由调用方降级。"""
    if not _memory_llm_available(config):
        return None
    assert config is not None
    agent = Agent(
        _build_memory_model(config),
        system_prompt=system_prompt,
        output_type=output_type,
        defer_model_check=True,
        retries=3,
    )
    try:
        # 后台记忆任务是串行的:一次卡住的调用会让后面所有抽取排队等待,所以给个上限
        result = asyncio.run(asyncio.wait_for(agent.run(json.dumps(payload, ensure_ascii=False)), timeout=_MEMORY_LLM_TIMEOUT_SECONDS))
        return result.output
    except asyncio.TimeoutError:
        logger.warning("memory llm %s timed out after %ss", kind, _MEMORY_LLM_TIMEOUT_SECONDS)
        return None
    except Exception as exc:
        logger.warning("memory llm %s agent degraded: %s", kind, exc)
        return None
