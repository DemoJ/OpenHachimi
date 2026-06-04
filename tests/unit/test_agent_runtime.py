import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import BinaryContent

from openhachimi_agent.agent.intent import PlanContinuationDecision
from openhachimi_agent.agent.intent import TaskFrame
from openhachimi_agent.interface.presenter import ToolProgressPresenter
from openhachimi_agent.service.agent_runtime.context import (
    AgentRunContext,
    has_active_todos,
    mark_turn_finished,
    mark_turn_started,
    should_route_new_turn,
)
from openhachimi_agent.service.agent_runtime.executor import _build_executor_message, _run_executor_with_vision_fallback
from openhachimi_agent.vision.preprocess import VisionPreprocessResult
from openhachimi_agent.transport.api_models import ArtifactRef, AttachmentRef
from openhachimi_agent.service.agent_runtime.router import should_route_message
from openhachimi_agent.service.agent_runtime.planner import needs_planning
from openhachimi_agent.service.agent_runtime.streaming import OperationStalledError, StreamEventItem, StreamStats, consume_stream_queue
from openhachimi_agent.service.agent_service import AgentService


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


def test_executor_message_with_fallback_vision_discourages_image_tools():
    attachment = AttachmentRef(
        id="att_1",
        filename="photo.jpg",
        content_type="image/jpeg",
        size_bytes=123,
        local_path=".tmp/attachments/telegram/u1/photo.jpg",
        source="telegram",
        kind="image",
    )
    vision_result = VisionPreprocessResult(
        mode="fallback",
        text_prefix="[图片附件识别结果]\n图中有一只猫。\n",
        consumed_attachment_ids=["att_1"],
    )

    message = _build_executor_message(None, "看看这张图", [attachment], vision_result)

    assert "图中有一只猫" in message
    assert "图片附件已由辅助视觉模型识别" in message
    assert "不要再调用 inspect_image、read_file、browser_navigate" in message
    assert "status: 已处理" in message


@pytest.mark.asyncio
async def test_direct_vision_http_error_retries_without_image_parts():
    class FailingVisionAgent:
        def __init__(self):
            self.messages = []

        async def run(self, message, **_kwargs):
            self.messages.append(message)
            if isinstance(message, list):
                raise ModelHTTPError(status_code=502, model_name="hachimi", body="")
            return SimpleNamespace(output="已降级回复")

    agent = FailingVisionAgent()
    attachment = AttachmentRef(
        id="att_1",
        filename="photo.jpg",
        content_type="image/jpeg",
        size_bytes=123,
        local_path=".tmp/attachments/telegram/u1/photo.jpg",
        source="telegram",
        kind="image",
    )
    vision_result = VisionPreprocessResult(
        mode="direct",
        direct_parts=[BinaryContent(data=b"image", media_type="image/jpeg", identifier="att_1")],
        consumed_attachment_ids=["att_1"],
    )
    config = SimpleNamespace(model_name="hachimi", openai_base_url="http://test", openai_api_key="key", agent_timeout_seconds=300)

    result, degraded = await _run_executor_with_vision_fallback(
        executor_agent=agent,
        task_frame_payload=None,
        message="看看这张图",
        attachments=[attachment],
        vision_result=vision_result,
        history=[],
        deps=SimpleNamespace(),
        config=config,
        stream=False,
        handle_stream_events=None,
    )

    assert result.output == "已降级回复"
    assert degraded.mode == "unavailable"
    assert degraded.direct_parts == []
    assert len(agent.messages) == 2
    assert isinstance(agent.messages[0], list)
    assert isinstance(agent.messages[1], str)
    assert "系统尝试将图片直接发送给主模型识别，但模型服务返回错误" in agent.messages[1]
    assert "主模型图片输入错误" in agent.messages[1]


def test_low_confidence_direct_task_does_not_need_planning():
    frame = TaskFrame(confidence=0.3, requires_plan=False, execution_mode="direct")

    assert needs_planning(frame) is False


def test_planned_execution_mode_needs_planning():
    frame = TaskFrame(confidence=0.9, requires_plan=False, execution_mode="planned")

    assert needs_planning(frame) is True


def test_presenter_passes_artifact_events():
    artifact = ArtifactRef(
        id="art_1",
        filename="report.md",
        content_type="text/markdown",
        size_bytes=5,
        local_path="report.md",
    )
    presenter = ToolProgressPresenter(mode="conversation")

    actions = presenter.handle_event(StreamEventItem(type="artifact", text="已生成文件：report.md", artifact=artifact, counted_as_output=False))

    assert len(actions) == 1
    assert actions[0].type == "artifact"
    assert actions[0].artifact == artifact


@pytest.mark.asyncio
async def test_stream_events_filters_tool_events_when_disabled():
    service = AgentService.__new__(AgentService)
    service.config = SimpleNamespace(show_tool_calls=False)

    async def fake_run_with_session(*_args, **_kwargs):
        yield StreamEventItem(type="tool", text="🔧 run_command", counted_as_output=False)
        yield StreamEventItem(type="text", text="完成")

    service._run_with_session = fake_run_with_session

    events = [event async for event in service.stream_events("hi")]

    assert [event.type for event in events] == ["text"]
    assert events[0].text == "完成"


@pytest.mark.asyncio
async def test_stream_artifact_event_not_counted_as_output(tmp_path):
    async def done_task():
        return None

    task = asyncio.create_task(done_task())
    queue = asyncio.Queue()
    artifact = ArtifactRef(id="art_1", filename="a.txt", content_type="text/plain", size_bytes=1, local_path="a.txt")
    await queue.put(StreamEventItem(type="artifact", text="已生成文件：a.txt", artifact=artifact, counted_as_output=False))
    ctx = _ctx({})
    config = SimpleNamespace(stream_idle_timeout_seconds=1, agent_timeout_seconds=300)
    stats = StreamStats()
    stream = consume_stream_queue(
        stream_queue=queue,
        task=task,
        config=config,
        role="default",
        session_id="session-1",
        start_time=time.perf_counter(),
        stats=stats,
        operation_state=ctx.operation_state,
    )

    item = await stream.__anext__()

    assert item.artifact == artifact
    assert stats.chunk_count == 0
    assert stats.output_chars == 0
    await stream.aclose()


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
