"""Local, per-request context guard without changing the stored execution history."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic_ai.messages import ModelMessage, ModelRequest
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel

from openhachimi_agent.context.token_estimate import estimate_messages_tokens, estimate_text_tokens


logger = logging.getLogger(__name__)


class ContextBudgetExceeded(ValueError):
    pass


def estimate_request_tokens(messages: list[ModelMessage], parameters: ModelRequestParameters) -> int:
    total = estimate_messages_tokens(messages)
    # Image/audio costs are provider-specific; this is a local text/schema safety check.
    instructions = getattr(parameters, "instruction_parts", None)
    if instructions is not None:
        total += sum(estimate_text_tokens(part.content) for part in instructions)
    else:
        for message in reversed(messages):
            if isinstance(message, ModelRequest) and message.instructions is not None:
                total += estimate_text_tokens(message.instructions)
                break
    for tool in [*parameters.function_tools, *parameters.output_tools]:
        payload = {"name": tool.name, "description": tool.description, "parameters": tool.parameters_json_schema}
        total += estimate_text_tokens(json.dumps(payload, ensure_ascii=False)) + 4
    output_object = getattr(parameters, "output_object", None)
    if output_object is not None:
        total += estimate_text_tokens(json.dumps(output_object.json_schema, ensure_ascii=False))
    return total


class ContextBudgetModel(WrapperModel):
    def __init__(self, wrapped, *, context_length: int, hard_ceiling_percent: float) -> None:
        super().__init__(wrapped)
        self.configured_context_length = context_length or 128000
        self.input_budget = int(self.configured_context_length * hard_ceiling_percent)

    def _check_budget(self, messages, settings, parameters) -> None:
        estimated = estimate_request_tokens(messages, parameters)
        output_reserve = int((settings or {}).get("max_tokens") or 0)
        if estimated < self.input_budget and estimated + output_reserve < self.configured_context_length:
            return
        logger.warning(
            "context request budget exceeded model=%s estimated_input=%d input_budget=%d output_reserve=%d window=%d",
            self.model_name, estimated, self.input_budget, output_reserve, self.configured_context_length,
        )
        raise ContextBudgetExceeded(
            f"当前请求的上下文估算约 {estimated:,} token，已达到安全预算 {self.input_budget:,}"
            f"（模型配置窗口 {self.configured_context_length:,}）。本次模型请求未发送，"
            "已执行的工具不会自动重放。请压缩会话（/compress）、缩小输入或新建会话后继续。"
        )

    async def request(self, messages, model_settings, model_request_parameters):
        self._check_budget(messages, model_settings, model_request_parameters)
        return await self.wrapped.request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None) -> AsyncIterator:
        self._check_budget(messages, model_settings, model_request_parameters)
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as response:
            yield response
