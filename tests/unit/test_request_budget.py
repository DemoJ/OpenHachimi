"""Every actual model request is budgeted, including inner tool-loop steps."""

from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import ToolDefinition

from openhachimi_agent.context import request_budget
from openhachimi_agent.context.request_budget import ContextBudgetExceeded, ContextBudgetModel, estimate_request_tokens


@pytest.fixture(autouse=True)
def local_tokens(monkeypatch):
    monkeypatch.setattr(request_budget, "estimate_text_tokens", len)
    monkeypatch.setattr(request_budget, "estimate_messages_tokens", lambda messages: sum(
        len(str(getattr(part, "content", ""))) + len(str(getattr(part, "args", ""))) + 4
        for message in messages for part in message.parts
    ))


def test_full_request_budget_counts_current_input_instructions_and_tool_schema():
    messages = [ModelRequest(parts=[UserPromptPart(content="current input")], instructions="legacy instructions")]
    tool = ToolDefinition(name="lookup", description="read something", parameters_json_schema={"type": "object"})
    parameters = ModelRequestParameters(function_tools=[tool])
    base = request_budget.estimate_messages_tokens(messages)
    assert estimate_request_tokens(messages, parameters) > base + len("legacy instructions")
    parameters.instruction_parts = [SimpleNamespace(content="new instructions")]
    new_total = estimate_request_tokens(messages, parameters)
    assert new_total == estimate_request_tokens(
        [ModelRequest(parts=messages[0].parts, instructions="ignored stale")], parameters
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_oversized_first_request_is_never_sent(stream):
    calls = 0

    def response(messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart(content="should not run")])

    async def response_stream(messages, info):
        nonlocal calls
        calls += 1
        yield "should not run"

    agent = Agent(ContextBudgetModel(
        FunctionModel(response, stream_function=response_stream), context_length=100, hard_ceiling_percent=0.9,
    ))

    async def consume(ctx, events):
        async for _ in events:
            pass

    kwargs = {"event_stream_handler": consume} if stream else {}
    with pytest.raises(ContextBudgetExceeded, match="未发送"):
        await agent.run("x" * 100, **kwargs)
    assert calls == 0


@pytest.mark.parametrize("stream", [False, True])
async def test_inner_tool_result_growth_is_checked_without_replaying_tool(stream):
    model_calls = 0
    tool_calls = 0

    def response(messages, info):
        nonlocal model_calls
        model_calls += 1
        return ModelResponse(parts=[ToolCallPart(tool_name="read_large", args={}, tool_call_id="c")])

    async def response_stream(messages, info):
        nonlocal model_calls
        model_calls += 1
        yield {0: DeltaToolCall(name="read_large", json_args="{}", tool_call_id="c")}

    agent = Agent(ContextBudgetModel(
        FunctionModel(response, stream_function=response_stream), context_length=1000, hard_ceiling_percent=0.9,
    ))

    @agent.tool_plain
    def read_large() -> str:
        nonlocal tool_calls
        tool_calls += 1
        return "x" * 1000

    async def consume(ctx, events):
        async for _ in events:
            pass

    kwargs = {"event_stream_handler": consume} if stream else {}
    with pytest.raises(ContextBudgetExceeded):
        await agent.run("small", **kwargs)
    assert model_calls == 1
    assert tool_calls == 1


def test_explicit_output_reserve_is_counted():
    model = ContextBudgetModel(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="ok")])),
                               context_length=100, hard_ceiling_percent=0.9)
    messages = [ModelRequest(parts=[UserPromptPart(content="x" * 60)])]
    with pytest.raises(ContextBudgetExceeded):
        model._check_budget(messages, {"max_tokens": 40}, ModelRequestParameters())
