"""停止/断流后,真实 SQLite 历史仍包含用户输入和已生成的正文。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from anyio import CancelScope
from pydantic_ai.messages import ModelRequest, ModelResponse, PartStartEvent, TextPart, UserPromptPart

from openhachimi_agent.service.agent_runtime import turn
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.storage.session_store import SessionStore


@pytest.fixture
def service(mock_config, monkeypatch):
    (mock_config.roles_dir / "default.md").write_text("Test role", encoding="utf-8")
    mock_config = replace(mock_config, memory=replace(mock_config.memory, enabled=False))
    with patch("openhachimi_agent.service.browser.BrowserManager"):
        service = AgentService(mock_config)
    monkeypatch.setattr(service, "_maybe_reload_mcp_toolsets", AsyncMock())
    monkeypatch.setattr(service, "_get_agent", lambda *a, **kw: None)
    monkeypatch.setattr(service, "_get_context_compressor", lambda sid: None)
    monkeypatch.setattr(service.process_manager, "terminate_session", lambda sid: 0)
    return service


def install_waiting_agent(monkeypatch, *, text="已生成的部分回复", cleanup_gate=None):
    started = asyncio.Event()

    async def resolve(service, ctx, deps, refreshed, result_holder):
        if text:
            async def events():
                yield PartStartEvent(index=0, part=TextPart(content=text))
            await ctx.stream_event_handler(None, events())
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if cleanup_gate is not None:
                await cleanup_gate.wait()
            raise

    monkeypatch.setattr(turn, "_resolve_turn_outcome", resolve)
    return started


async def collect(events):
    return [event async for event in events]


def assert_saved(service, sid, *, text="已生成的部分回复", count=1):
    # 新建存储实例读取磁盘,不依赖当前轮次的内存状态。
    reopened = SessionStore(service.config.memory_dir / "sessions.sqlite3")
    _, saved = reopened.load_messages("default", sid)
    prompts = [p.content for msg in saved if isinstance(msg, ModelRequest)
               for p in msg.parts if isinstance(p, UserPromptPart)]
    replies = [p.content for msg in saved if isinstance(msg, ModelResponse)
               for p in msg.parts if isinstance(p, TextPart)]
    assert prompts == ["停止也应该保存"] * count
    assert replies == ([text] if text else [])
    assert reopened.list_sessions("default", channel="webui")[0]["session_id"] == sid
    assert service.list_sessions("default", channel="webui")["sessions"][0]["preview"] == "停止也应该保存"
    displayed = service.get_session_messages("default", sid)["messages"]
    assert displayed[0].content == "停止也应该保存"
    if text:
        assert displayed[-1].content == text


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("text", ["", "已生成的部分回复"])
@pytest.mark.asyncio
async def test_stop_saves_before_return_and_does_not_duplicate(service, monkeypatch, stream, text):
    started = install_waiting_agent(monkeypatch, text=text)
    sid = service.new_session_for_channel("default", "webui").session_id
    kwargs = {"channel_context": {"channel_code": "webui"}}
    request = service.stream_events("停止也应该保存", "default", sid, **kwargs) if stream else None
    consumer = asyncio.create_task(collect(request) if stream else service.send_message("停止也应该保存", "default", sid, **kwargs))
    await asyncio.wait_for(started.wait(), timeout=2)

    await asyncio.wait_for(service.stop_session(sid), timeout=2)
    assert_saved(service, sid, text=text)
    await asyncio.wait_for(consumer, timeout=2)
    await service.stop_session(sid)
    assert_saved(service, sid, text=text)
    assert not service._running_tasks
    assert not service._turn_checkpoints


@pytest.mark.asyncio
async def test_stop_saves_without_waiting_for_model_cleanup(service, monkeypatch):
    cleanup_gate = asyncio.Event()
    started = install_waiting_agent(monkeypatch, cleanup_gate=cleanup_gate)
    sid = service.new_session_for_channel("default", "webui").session_id
    consumer = asyncio.create_task(collect(service.stream_events("停止也应该保存", "default", sid)))
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        await asyncio.wait_for(service.stop_session(sid), timeout=0.5)
        assert_saved(service, sid)
        assert not consumer.done()
    finally:
        cleanup_gate.set()
        await asyncio.wait_for(consumer, timeout=2)


@pytest.mark.asyncio
async def test_closing_stream_saves_partial_reply(service, monkeypatch):
    started = install_waiting_agent(monkeypatch)
    sid = service.new_session_for_channel("default", "webui").session_id
    events = service.stream_events("停止也应该保存", "default", sid)
    assert (await anext(events)).text == "已生成的部分回复"
    await started.wait()
    await events.aclose()
    assert_saved(service, sid)
    assert not service._running_tasks


@pytest.mark.parametrize("anyio_cancel", [False, True])
@pytest.mark.asyncio
async def test_cancelled_http_consumer_saves_partial_reply(service, monkeypatch, anyio_cancel):
    started = install_waiting_agent(monkeypatch)
    sid = service.new_session_for_channel("default", "webui").session_id
    scopes = []

    async def consume():
        with CancelScope() as scope:
            scopes.append(scope)
            await collect(service.stream_events("停止也应该保存", "default", sid))

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=2)
    if anyio_cancel:
        scopes[0].cancel()
        await asyncio.wait_for(consumer, timeout=2)
    else:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert_saved(service, sid)
    assert not service._running_tasks


@pytest.mark.asyncio
async def test_normal_completion_and_next_turn_do_not_duplicate_stopped_messages(service, monkeypatch):
    started = install_waiting_agent(monkeypatch)
    sid = service.new_session_for_channel("default", "webui").session_id
    consumer = asyncio.create_task(collect(service.stream_events("停止也应该保存", "default", sid)))
    await asyncio.wait_for(started.wait(), timeout=2)
    await service.stop_session(sid)
    await consumer

    async def finish(service, ctx, deps, refreshed, result_holder):
        assert len(ctx.history) == 2
        messages = [*ctx.history, ModelRequest(parts=[UserPromptPart(content="继续")]),
                    ModelResponse(parts=[TextPart(content="正常完成")])]
        result_holder["result"] = SimpleNamespace(output="正常完成", all_messages=lambda: messages)

    monkeypatch.setattr(turn, "_resolve_turn_outcome", finish)
    await collect(service.stream_events("继续", "default", sid))
    reopened = SessionStore(service.config.memory_dir / "sessions.sqlite3")
    _, saved = reopened.load_messages("default", sid)
    assert len(saved) == 4
    assert [p.content for msg in saved for p in msg.parts] == [
        "停止也应该保存", "已生成的部分回复", "继续", "正常完成",
    ]


@pytest.mark.asyncio
async def test_stop_during_initialization_still_saves_user_message(service, monkeypatch):
    initializing = asyncio.Event()

    async def slow_init():
        initializing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_maybe_reload_mcp_toolsets", slow_init)
    sid = service.new_session_for_channel("default", "webui").session_id
    consumer = asyncio.create_task(collect(service.stream_events("停止也应该保存", "default", sid)))
    await asyncio.wait_for(initializing.wait(), timeout=2)
    await service.stop_session(sid)
    assert_saved(service, sid, text="")
    await asyncio.wait_for(consumer, timeout=2)


@pytest.mark.asyncio
async def test_concurrent_stops_only_save_once(service, monkeypatch):
    started = install_waiting_agent(monkeypatch)
    sid = service.new_session_for_channel("default", "webui").session_id
    consumer = asyncio.create_task(collect(service.stream_events("停止也应该保存", "default", sid)))
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.gather(service.stop_session(sid), service.stop_session(sid))
    await asyncio.wait_for(consumer, timeout=2)
    assert_saved(service, sid)


@pytest.mark.asyncio
async def test_stop_reports_database_write_failure(service, monkeypatch):
    started = install_waiting_agent(monkeypatch)
    sid = service.new_session_for_channel("default", "webui").session_id
    consumer = asyncio.create_task(collect(service.stream_events("停止也应该保存", "default", sid)))
    await asyncio.wait_for(started.wait(), timeout=2)

    def fail_save(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(service.session_store, "save_messages", fail_save)
    with pytest.raises(RuntimeError, match="消息保存失败"):
        await service.stop_session(sid)
    await asyncio.wait_for(consumer, timeout=2)
    assert not service._running_tasks


@pytest.mark.asyncio
async def test_stop_validated_stream_saves_replacement_as_unverified_draft(service, monkeypatch):
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import ModelRetry
    from pydantic_ai.models.function import FunctionModel
    from openhachimi_agent.agent.answer_attempts import ANSWER_STATUS_KEY, mark_latest_answer

    started = asyncio.Event()
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "rejected first answer"
        else:
            yield "replacement draft"
            started.set()
            await asyncio.Event().wait()

    agent = Agent(FunctionModel(stream_function=model), retries=2)

    @agent.output_validator
    def validate(ctx, result):
        if result == "rejected first answer":
            mark_latest_answer(ctx.messages, "rejected")
            raise ModelRetry("verify first")
        return result

    monkeypatch.setattr(service, "_get_agent", lambda *a, **kw: agent)
    sid = service.new_session_for_channel("default", "webui").session_id
    consumer = asyncio.create_task(collect(service.stream_events("停止也应该保存", "default", sid)))
    await asyncio.wait_for(started.wait(), timeout=2)
    await service.stop_session(sid)
    await asyncio.wait_for(consumer, timeout=2)
    _, saved = SessionStore(service.config.memory_dir / "sessions.sqlite3").load_messages("default", sid)
    replies = [m for m in saved if isinstance(m, ModelResponse)]
    assert len(replies) == 1
    assert replies[0].parts[0].content == "[未完成且未经验证的草稿]\nreplacement draft"
    assert replies[0].metadata[ANSWER_STATUS_KEY] == "interrupted"
    assert "rejected first answer" not in service.get_session_messages("default", sid)["messages"][-1].content
