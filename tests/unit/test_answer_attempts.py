"""Rejected answer candidates stay in the audit trail, not in public replies."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RequestUsage,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import DeferredToolRequests

from openhachimi_agent.agent.answer_attempts import (
    ANSWER_STATUS_KEY,
    answer_status,
    mark_latest_answer,
    rejected_answer_indices,
    stamp_answer_attempts,
)
from openhachimi_agent.agent.factory import _validate_execution_result
from openhachimi_agent.service.agent_runtime.context import AgentRunContext, OperationState
from openhachimi_agent.service.agent_runtime.history_display import build_display_messages
from openhachimi_agent.service.agent_runtime.main_agent import run_main_agent, resume_main_agent
from openhachimi_agent.service.agent_runtime.streaming import (
    ValidatedAnswerStream,
    build_stream_event_handler,
)
from openhachimi_agent.service.agent_runtime.turn_postprocess import _persist_turn
from openhachimi_agent.storage.session_store import SessionStore


def render_user(message):
    return [{"role": "user", "content": p.content} for p in message.parts if isinstance(p, UserPromptPart)]


def test_legacy_output_retry_hides_only_rejected_text_and_keeps_audit():
    usage = RequestUsage(input_tokens=100, output_tokens=10)
    rejected = ModelResponse(parts=[TextPart(content="candidate A")], usage=usage)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="question")]),
        rejected,
        ModelRequest(parts=[RetryPromptPart(content="verify first")]),
        ModelResponse(parts=[ToolCallPart(tool_name="git_status", args={}, tool_call_id="c")], usage=usage),
        ModelRequest(parts=[ToolReturnPart(tool_name="git_status", content="clean", tool_call_id="c")]),
        ModelResponse(parts=[TextPart(content="answer B")], usage=usage),
    ]
    display = build_display_messages(enumerate(messages), render_user)
    assert display[-1]["content"] == "answer B"
    assert len(display[-1]["tool_calls"]) == 1
    assert display[-1]["tokens"]["input"] == 300
    assert rejected.parts[0].content == "candidate A"
    assert rejected.metadata is None


def test_tool_retry_does_not_reject_commentary_or_cross_user_turns():
    messages = [
        ModelResponse(parts=[TextPart(content="checking"), ToolCallPart(tool_name="read_file", args={}, tool_call_id="c")]),
        ModelRequest(parts=[RetryPromptPart(content="bad path", tool_name="read_file", tool_call_id="c")]),
        ModelRequest(parts=[UserPromptPart(content="new question")]),
        ModelRequest(parts=[RetryPromptPart(content="no output")]),
    ]
    assert rejected_answer_indices(messages) == set()


def test_persistence_status_is_additive_and_only_stamps_current_turn():
    old = ModelResponse(parts=[TextPart(content="old")])
    current = ModelResponse(parts=[TextPart(content="candidate")], metadata={"kept": True})
    messages = [old, ModelRequest(parts=[RetryPromptPart(content="old retry")]), current,
                ModelRequest(parts=[RetryPromptPart(content="new retry")])]
    stamp_answer_attempts(messages, start_index=2)
    assert old.metadata is None
    assert current.metadata == {"kept": True, ANSWER_STATUS_KEY: "rejected"}


def test_factory_validator_marks_candidate_rejected_and_committed():
    first = ModelResponse(parts=[TextPart(content="A")])
    state = {
        "execution_ledger": [{"seq": 1, "tool_name": "web_fetch", "status": "failed", "result_preview": "403"}],
        "current_turn_ledger_start_seq": 0,
    }
    ctx = SimpleNamespace(deps=SimpleNamespace(session_state=state, session_id="s", session_store=None), messages=[first])
    with pytest.raises(ModelRetry):
        _validate_execution_result(ctx, "A")
    assert answer_status(first) == "rejected"
    second = ModelResponse(parts=[TextPart(content="B")])
    ctx.messages.append(second)
    assert _validate_execution_result(ctx, "B") == "B"
    assert answer_status(second) == "committed"


@pytest.mark.asyncio
async def test_candidate_stream_publishes_tools_but_not_text_or_failed_candidates():
    queue = asyncio.Queue()
    output = []
    state = OperationState()
    stream = ValidatedAnswerStream(build_stream_event_handler(queue, state, output=output), state)

    async def events():
        yield PartStartEvent(index=0, part=TextPart(content="candidate A"))
        yield PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" more"))
        yield FunctionToolCallEvent(part=ToolCallPart(tool_name="git_status", args={}))
        yield PartStartEvent(index=0, part=TextPart(content="candidate B"))

    await stream(None, events())
    assert stream.tool_calls_started
    assert [item.type for item in output] == ["tool"]
    assert queue.qsize() == 1
    await stream.commit(SimpleNamespace(output="accepted B"))
    assert [item.text for item in output if item.type == "text"] == ["accepted B"]
    await stream.commit(SimpleNamespace(output=DeferredToolRequests()))
    assert queue.qsize() == 2


@pytest.mark.asyncio
async def test_real_agent_retry_only_publishes_accepted_answer(mock_config, mock_agent_deps):
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        yield "candidate A" if calls == 1 else "accepted B"

    agent = Agent(FunctionModel(stream_function=model), retries=2)

    @agent.output_validator
    def validate(ctx, text):
        if text == "candidate A":
            mark_latest_answer(ctx.messages, "rejected")
            raise ModelRetry("verification needed")
        mark_latest_answer(ctx.messages, "committed")
        return text

    queue = asyncio.Queue()
    public = []
    ctx = AgentRunContext(
        config=mock_config, role="default", session_id="s", message="question", attachments=[],
        history=[], deps=mock_agent_deps, session_state=mock_agent_deps.session_state,
        stream=True, stream_queue=queue,
    )
    ctx.stream_event_handler = build_stream_event_handler(queue, ctx.operation_state, output=public)
    outcome = await run_main_agent(ctx, lambda *a, **kw: agent)
    assert calls == 2
    assert [item.text for item in public if item.type == "text"] == ["accepted B"]
    raw = outcome.result.all_messages()
    assert any(isinstance(p, TextPart) and p.content == "candidate A" for m in raw for p in m.parts)
    assert build_display_messages(enumerate(raw), render_user)[-1]["content"] == "accepted B"


@pytest.mark.asyncio
async def test_resume_uses_the_same_validated_answer_stream(mock_config, mock_agent_deps):
    calls = 0

    async def run(_prompt, **kwargs):
        nonlocal calls
        calls += 1
        async def events():
            yield PartStartEvent(index=0, part=TextPart(content="rejected preview"))
        await kwargs["event_stream_handler"](None, events())
        return SimpleNamespace(output="accepted resume")

    mock_agent_deps.session_state["_user_clarification"] = {"tool_call_id": "q", "question": "which?"}
    queue = asyncio.Queue()
    public = []
    ctx = AgentRunContext(
        config=mock_config, role="default", session_id="s", message="choice", attachments=[], history=[],
        deps=mock_agent_deps, session_state=mock_agent_deps.session_state, stream=True, stream_queue=queue,
    )
    ctx.stream_event_handler = build_stream_event_handler(queue, ctx.operation_state, output=public)
    await resume_main_agent(ctx, lambda *a, **kw: SimpleNamespace(run=run))
    assert calls == 1
    assert [item.text for item in public] == ["accepted resume"]


def test_deferred_question_does_not_publish_hidden_commentary_on_reload():
    messages = [ModelRequest(parts=[UserPromptPart(content="q")]), ModelResponse(parts=[
        TextPart(content="hidden commentary"),
        ToolCallPart(tool_name="clarify_user", args={"question": "Which target?"}, tool_call_id="c"),
    ])]
    stamp_answer_attempts(messages, committed=False)
    display = build_display_messages(enumerate(messages), render_user)
    assert display[-1]["content"] == "Which target?"
    assert messages[-1].parts[0].content == "hidden commentary"


@pytest.mark.asyncio
async def test_rejected_candidates_remain_auditable_after_sqlite_roundtrip(mock_config, mock_agent_deps, monkeypatch):
    import openhachimi_agent.service.agent_runtime.turn_postprocess as module

    store = SessionStore(mock_config.memory_dir / "answer-test.sqlite3")
    sid = SessionStore.new_session_id()
    messages = [ModelRequest(parts=[UserPromptPart(content="q")]),
                ModelResponse(parts=[TextPart(content="A")]),
                ModelRequest(parts=[RetryPromptPart(content="verify")]),
                ModelResponse(parts=[TextPart(content="B")])]
    service = SimpleNamespace(session_store=store)
    ctx = SimpleNamespace(context_compressor=None)
    monkeypatch.setattr(module, "_stamp_turn_context", lambda *a, **kw: None)
    await _persist_turn(
        service, ctx, SimpleNamespace(all_messages=lambda: messages), mock_agent_deps,
        role="default", actual_session_id=sid, latest_scope=None, resolved_channel_code="webui",
        message="q", history=[],
    )
    _, saved = store.load_messages("default", sid)
    assert saved[1].parts[0].content == "A"
    assert saved[1].metadata[ANSWER_STATUS_KEY] == "rejected"
    assert build_display_messages(enumerate(saved), render_user)[-1]["content"] == "B"


@pytest.mark.asyncio
async def test_real_tool_loop_keeps_activity_live_and_commits_only_final_text():
    calls = 0
    queue = asyncio.Queue()
    public = []
    state = OperationState()
    stream = ValidatedAnswerStream(build_stream_event_handler(queue, state, output=public), state)

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "let me check"
            yield {0: DeltaToolCall(name="lookup", json_args="{}", tool_call_id="c")}
        else:
            yield "final answer"

    agent = Agent(FunctionModel(stream_function=model))

    @agent.tool_plain
    def lookup() -> str:
        assert any(item.type == "tool" for item in public)
        assert not any(item.type == "text" for item in public)
        return "data"

    result = await agent.run("q", event_stream_handler=stream)
    await stream.commit(result)
    assert calls == 2
    assert [item.text for item in public if item.type == "text"] == ["final answer"]
    messages = result.all_messages()
    stamp_answer_attempts(messages)
    assert build_display_messages(enumerate(messages), render_user)[-1]["content"] == "final answer"
    assert messages[1].metadata[ANSWER_STATUS_KEY] == "internal"


@pytest.mark.asyncio
async def test_cancelled_real_stream_keeps_only_current_unrejected_draft():
    started = asyncio.Event()
    state = OperationState()
    queue = asyncio.Queue()
    stream = ValidatedAnswerStream(build_stream_event_handler(queue, state), state)

    async def model(messages, info):
        yield "partial draft"
        started.set()
        await asyncio.Event().wait()

    agent = Agent(FunctionModel(stream_function=model))
    task = asyncio.create_task(agent.run("q", event_stream_handler=stream))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert queue.empty()
    assert [item.text for item in stream.interrupted_output()] == ["[未完成且未经验证的草稿]\npartial draft"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rejected = ModelResponse(parts=[TextPart(content="partial draft")])
    mark_latest_answer([rejected], "rejected")
    stream.messages = [rejected]
    assert stream.interrupted_output() == []
    stream.messages = [rejected, ModelRequest(parts=[RetryPromptPart(content="retry")])]
    assert stream.interrupted_output() == []


@pytest.mark.asyncio
async def test_network_retry_does_not_publish_abandoned_answer(monkeypatch):
    import httpx
    import openhachimi_agent.service.agent_runtime.main_agent as module

    calls = 0
    state = OperationState()
    queue = asyncio.Queue()
    public = []
    stream = ValidatedAnswerStream(build_stream_event_handler(queue, state, output=public), state)

    async def run(**kwargs):
        nonlocal calls
        calls += 1
        async def events():
            yield PartStartEvent(index=0, part=TextPart(content=f"draft {calls}"))
        await kwargs["handle_stream_events"](None, events())
        if calls == 1:
            raise httpx.RemoteProtocolError("disconnected")
        return SimpleNamespace(output="accepted after retry")

    monkeypatch.setattr(module, "run_main_agent_once", run)
    vision = SimpleNamespace(direct_parts=[], mode="none", text_prefix="", consumed_attachment_ids=[], attachment_statuses=[], errors=[])
    result = await module._run_main_with_llm_retry(
        main_agent=None, message="q", attachments=[], vision_result=vision, history=[],
        deps=SimpleNamespace(role_name="r", session_id="s", session_state={}),
        config=SimpleNamespace(model_name="m"), stream=True, handle_stream_events=stream,
    )
    await stream.commit(result)
    assert calls == 2
    assert [item.text for item in public] == ["accepted after retry"]
    assert stream.interrupted_output() == []


@pytest.mark.asyncio
async def test_network_error_after_tool_never_replays_the_whole_run(monkeypatch):
    import httpx
    import openhachimi_agent.service.agent_runtime.main_agent as module

    calls = 0
    deps = SimpleNamespace(role_name="r", session_id="s", session_state={})

    async def run(**kwargs):
        nonlocal calls
        calls += 1
        deps.session_state["execution_ledger"] = [{"seq": 1, "tool_name": "write_file", "status": "succeeded"}]
        raise httpx.RemoteProtocolError("disconnected after write")

    monkeypatch.setattr(module, "run_main_agent_once", run)
    vision = SimpleNamespace(direct_parts=[], mode="none", text_prefix="", consumed_attachment_ids=[], attachment_statuses=[], errors=[])
    with pytest.raises(httpx.RemoteProtocolError):
        await module._run_main_with_llm_retry(
            main_agent=None, message="q", attachments=[], vision_result=vision, history=[], deps=deps,
            config=SimpleNamespace(model_name="m"), stream=False, handle_stream_events=None,
        )
    assert calls == 1


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_explicit_tool_failure_survives_real_model_loop_and_storage_normalization(streaming):
    from openhachimi_agent.agent.execution import ToolOutcome, ToolResult
    from openhachimi_agent.service.agent_runtime.streaming import normalize_tool_outcomes

    def model(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="unavailable", args={}, tool_call_id="c")])
        return ModelResponse(parts=[TextPart(content="browser unavailable")])

    async def model_stream(messages, info):
        if len(messages) == 1:
            yield {0: DeltaToolCall(name="unavailable", json_args="{}", tool_call_id="c")}
        else:
            yield "browser unavailable"

    agent = Agent(FunctionModel(model, stream_function=model_stream))

    @agent.tool_plain
    def unavailable() -> str:
        return ToolResult("cannot launch browser", outcome=ToolOutcome("failed"))

    state = OperationState()
    queue = asyncio.Queue()
    trace = []
    kwargs = {"event_stream_handler": build_stream_event_handler(queue, state, trace=trace)} if streaming else {}
    result = await agent.run("q", **kwargs)
    messages = result.all_messages()
    normalize_tool_outcomes(messages)
    returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    assert returned[0].outcome == "failed"
    if streaming:
        assert trace[0].outcome == "failed"


@pytest.mark.asyncio
async def test_immediate_stop_preserves_replacement_draft_before_cleanup():
    started = asyncio.Event()
    calls = 0
    state = OperationState()
    stream = ValidatedAnswerStream(build_stream_event_handler(asyncio.Queue(), state), state)

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "rejected A"
        else:
            yield "replacement B"
            started.set()
            await asyncio.Event().wait()

    agent = Agent(FunctionModel(stream_function=model), retries=2)

    @agent.output_validator
    def validate(ctx, result):
        if result == "rejected A":
            mark_latest_answer(ctx.messages, "rejected")
            raise ModelRetry("verify first")
        return result

    task = asyncio.create_task(agent.run("q", event_stream_handler=stream))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    assert [item.text for item in stream.interrupted_output()] == ["[未完成且未经验证的草稿]\nreplacement B"]
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_vision_fallback_cannot_replay_tools_even_with_saturated_ledger(monkeypatch):
    from pydantic_ai.exceptions import ModelHTTPError
    from openhachimi_agent.agent.execution import _append_ledger_event
    import openhachimi_agent.service.agent_runtime.main_agent as module

    calls = 0
    state = {"execution_ledger": [{"seq": i + 1} for i in range(200)]}
    deps = SimpleNamespace(role_name="r", session_id="s", session_state=state)

    async def run(**kwargs):
        nonlocal calls
        calls += 1
        _append_ledger_event(state, tool_name="write_file", status="succeeded")
        raise ModelHTTPError(status_code=400, model_name="m", body={"error": "image rejected"})

    monkeypatch.setattr(module, "run_main_agent_once", run)
    monkeypatch.setattr(module, "_build_main_message", lambda *args: "q")
    with pytest.raises(ModelHTTPError):
        await module._run_main_with_vision_fallback(
            main_agent=None, message="q", attachments=[], vision_result=SimpleNamespace(direct_parts=["image"]),
            history=[], deps=deps, config=SimpleNamespace(model_name="m"), stream=False, handle_stream_events=None,
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_plain_tool_without_ledger_is_not_replayed_after_network_error(mock_config, mock_agent_deps):
    import httpx
    from openhachimi_agent.service.agent_runtime.main_agent import _run_main_with_llm_retry

    model_calls = 0
    tool_calls = 0

    def response(messages, info):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="external_write", args={}, tool_call_id="c")])
        raise httpx.RemoteProtocolError("disconnected after external action")

    agent = Agent(FunctionModel(response))

    @agent.tool_plain
    def external_write() -> str:
        nonlocal tool_calls
        tool_calls += 1
        return "sent"

    vision = SimpleNamespace(direct_parts=[], mode="none", text_prefix="", consumed_attachment_ids=[], attachment_statuses=[], errors=[])
    with pytest.raises(httpx.RemoteProtocolError):
        await _run_main_with_llm_retry(
            main_agent=agent, message="q", attachments=[], vision_result=vision, history=[],
            deps=mock_agent_deps, config=mock_config, stream=False, handle_stream_events=None,
        )
    assert tool_calls == 1
    assert model_calls == 2
