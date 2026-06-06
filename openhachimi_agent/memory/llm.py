"""长期记忆 LLM 调用封装。"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.core.config import AppConfig

logger = logging.getLogger(__name__)


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
    """Run structured long-term memory extraction through pydantic_ai."""
    if not _memory_llm_available(config):
        return None
    assert config is not None
    agent = Agent(
        _build_memory_model(config),
        system_prompt=system_prompt,
        output_type=MemoryExtractionOutput,
        defer_model_check=True,
        retries=3,
    )
    try:
        result = agent.run_sync(json.dumps(payload, ensure_ascii=False))
        return result.output
    except Exception as exc:
        logger.debug("memory llm extraction agent degraded: %s", exc)
        return None


def run_memory_summary(
    config: AppConfig | None,
    *,
    system_prompt: str,
    payload: dict[str, Any],
) -> MemorySummaryOutput | None:
    """Run structured long-term memory summarization through pydantic_ai."""
    if not _memory_llm_available(config):
        return None
    assert config is not None
    agent = Agent(
        _build_memory_model(config),
        system_prompt=system_prompt,
        output_type=MemorySummaryOutput,
        defer_model_check=True,
        retries=3,
    )
    try:
        result = agent.run_sync(json.dumps(payload, ensure_ascii=False))
        return result.output
    except Exception as exc:
        logger.debug("memory llm summary agent degraded: %s", exc)
        return None
