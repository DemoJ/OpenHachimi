from datetime import datetime, timezone
from types import SimpleNamespace

from openhachimi_agent.tools.scheduler import create_delayed_task, create_scheduled_task
from openhachimi_agent.scheduler.store import ScheduledTaskStore


class FakeCtx:
    def __init__(self, config, session_id="session-1"):
        self.deps = SimpleNamespace(config=config, session_id=session_id)


def test_create_delayed_task_creates_once_task(mock_config):
    ctx = FakeCtx(mock_config, session_id="user-session")

    result = create_delayed_task(ctx, prompt="回复：定时任务已触发", delay_seconds=60, name="提醒")

    task = ScheduledTaskStore(mock_config.scheduler.db_path).get_task(result["id"])
    assert task is not None
    assert task.name == "提醒"
    assert task.prompt == "回复：定时任务已触发"
    assert task.schedule_type == "once"
    assert task.session_id == "user-session"
    assert task.metadata["source"] == "agent_tool"
    assert task.next_run_at > datetime.now(timezone.utc)


def test_create_scheduled_task_creates_interval_task(mock_config):
    ctx = FakeCtx(mock_config, session_id="user-session")

    result = create_scheduled_task(
        ctx,
        name="循环提醒",
        prompt="回复：循环触发",
        schedule_type="interval",
        schedule_expr="10m",
    )

    task = ScheduledTaskStore(mock_config.scheduler.db_path).get_task(result["id"])
    assert task is not None
    assert task.schedule_type == "interval"
    assert task.schedule_expr == "10m"
    assert task.session_id == "user-session"
