import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from openhachimi_agent.agent.intent import PlanContinuationDecision
from openhachimi_agent.service.agent_runtime.context import (
    AgentRunContext,
    has_active_todos,
    mark_turn_finished,
    mark_turn_started,
    should_route_new_turn,
)
from openhachimi_agent.service.agent_runtime.executor import _build_executor_message
from openhachimi_agent.transport.api_models import AttachmentRef
from openhachimi_agent.service.agent_runtime.router import should_route_message
from openhachimi_agent.service.agent_runtime.streaming import OperationStalledError, StreamStats, consume_stream_queue


class FakeContinuationAgent:
    def __init__(self, action):
        self.action = action

    async def run(self, _prompt):
        return SimpleNamespace(
            output=PlanContinuationDecision(action=self.action, confidence=0.9, rationale="test")
        )


def _get_agent_for_action(action):
    def get_agent(_role, agent_type):
        assert agent_type == "continuation"
        return FakeContinuationAgent(action)

    return get_agent


def _ctx(session_state, message="帮我处理任务"):
    return AgentRunContext(
        config=SimpleNamespace(),
        role="default",
        session_id="session-1",
        message=message,
        attachments=[],
        history=[],
        deps=SimpleNamespace(),
        session_state=session_state,
        stream=False,
    )


@pytest.mark.asyncio
async def test_turn_completion_marker_controls_routing():
    session_state = {}
    get_agent = _get_agent_for_action("start_new_task")

    assert should_route_new_turn(session_state) is True
    assert await should_route_message(_ctx(session_state), get_agent) is True

    mark_turn_started(session_state)
    assert should_route_new_turn(session_state) is False
    assert await should_route_message(_ctx(session_state), get_agent) is False

    mark_turn_finished(session_state)
    assert should_route_new_turn(session_state) is True
    assert await should_route_message(_ctx(session_state), get_agent) is True


@pytest.mark.asyncio
async def test_active_todos_do_not_hijack_new_message():
    todo_state = SimpleNamespace(
        is_active=True,
        tasks={1: SimpleNamespace(status="in-progress")},
    )
    session_state = {"todo_state": todo_state}

    assert has_active_todos(session_state) is True
    assert await should_route_message(
        _ctx(session_state, "这是一个新问题"),
        _get_agent_for_action("start_new_task"),
    ) is True
    assert todo_state.is_active is False
    assert session_state["plan_status"] == "suspended"


@pytest.mark.asyncio
async def test_ai_continue_decision_reuses_active_todos():
    todo_state = SimpleNamespace(
        is_active=True,
        tasks={1: SimpleNamespace(status="in-progress")},
    )
    session_state = {"todo_state": todo_state}

    assert await should_route_message(
        _ctx(session_state, "麻烦接着处理剩下的事情"),
        _get_agent_for_action("continue_active_plan"),
    ) is False
    assert todo_state.is_active is True


@pytest.mark.asyncio
async def test_ai_resume_decision_restores_suspended_plan():
    todo_state = SimpleNamespace(
        is_active=False,
        tasks={1: SimpleNamespace(status="in-progress")},
    )
    session_state = {
        "todo_state": todo_state,
        "suspended_plan": {"reason": "operation_stalled"},
    }

    assert await should_route_message(
        _ctx(session_state, "按你判断恢复之前那件事"),
        _get_agent_for_action("resume_suspended_plan"),
    ) is False
    assert todo_state.is_active is True
    assert session_state["plan_status"] == "active"


def test_executor_message_preserves_task_frame_contract():
    task_frame = {
        "goal": "只处理指定文件",
        "target_entities": [{"type": "file", "value": "a.py"}],
        "invariants": ["不能替换目标文件"],
    }

    message = _build_executor_message(task_frame, "修复 a.py")

    assert "TaskFrame" in message
    assert "只处理指定文件" in message
    assert "用户原始任务：修复 a.py" in message


def test_executor_message_without_task_frame_is_raw_message():
    assert _build_executor_message(None, "直接回答") == "直接回答"


def test_executor_message_with_attachments_adds_safe_summary():
    attachment = AttachmentRef(
        id="att_1",
        filename="photo.jpg",
        content_type="image/jpeg",
        size_bytes=123,
        local_path=".tmp/attachments/telegram/u1/photo.jpg",
        source="telegram",
        kind="image",
    )

    message = _build_executor_message(None, "看看这张图", [attachment])

    assert "看看这张图" in message
    assert "att_1" in message
    assert "photo.jpg" in message
    assert ".tmp/attachments/telegram/u1/photo.jpg" in message
    assert "不要臆测附件内容" in message


@pytest.mark.asyncio
async def test_stream_idle_logs_heartbeat_without_yielding_message(caplog):
    async def long_running():
        await asyncio.sleep(0.05)

    task = asyncio.create_task(long_running())
    ctx = _ctx({})
    config = SimpleNamespace(stream_idle_timeout_seconds=0.01, agent_timeout_seconds=300)
    stream = consume_stream_queue(
        stream_queue=asyncio.Queue(),
        task=task,
        config=config,
        role="default",
        session_id="session-1",
        start_time=time.perf_counter(),
        stats=StreamStats(),
        operation_state=ctx.operation_state,
    )

    with caplog.at_level("INFO"):
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    assert "chat heartbeat" in caplog.text
    assert task.done()
    await stream.aclose()


@pytest.mark.asyncio
async def test_stream_watchdog_cancels_stalled_operation():
    async def long_running():
        await asyncio.sleep(10)

    task = asyncio.create_task(long_running())
    ctx = _ctx({})
    ctx.operation_state.start("tool", "browser_navigate")
    ctx.operation_state.last_progress_at = time.perf_counter() - 121
    config = SimpleNamespace(stream_idle_timeout_seconds=0.01, agent_timeout_seconds=300)
    stream = consume_stream_queue(
        stream_queue=asyncio.Queue(),
        task=task,
        config=config,
        role="default",
        session_id="session-1",
        start_time=time.perf_counter(),
        stats=StreamStats(),
        operation_state=ctx.operation_state,
    )

    with pytest.raises(OperationStalledError):
        await stream.__anext__()
    assert task.cancelled()
