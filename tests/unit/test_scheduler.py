import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from openhachimi_agent.scheduler.models import ScheduleType
from openhachimi_agent.scheduler.runner import ScheduledTaskRunner
from openhachimi_agent.scheduler.scheduler import TaskScheduler
from openhachimi_agent.scheduler.store import ScheduledTaskStore
from openhachimi_agent.scheduler.time_utils import compute_next_run, parse_interval_seconds


def test_parse_interval_seconds_supports_units():
    assert parse_interval_seconds("30") == 30
    assert parse_interval_seconds("10m") == 600
    assert parse_interval_seconds("2h") == 7200
    assert parse_interval_seconds("1d") == 86400


def test_compute_next_run_once_and_interval():
    now = datetime(2026, 5, 27, 8, 0, tzinfo=timezone.utc)

    once = compute_next_run(ScheduleType.ONCE, "2026-05-27T09:00:00+00:00", after=now)
    interval = compute_next_run(ScheduleType.INTERVAL, "15m", after=now)
    expired = compute_next_run(ScheduleType.ONCE, "2026-05-27T07:00:00+00:00", after=now)

    assert once == datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc)
    assert interval == now + timedelta(minutes=15)
    assert expired == datetime(2026, 5, 27, 7, 0, tzinfo=timezone.utc)


def test_store_crud_and_claim(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    now = datetime.now(timezone.utc)
    task = store.create_task(
        name="提醒",
        prompt="提醒我喝水",
        schedule_type="once",
        schedule_expr=(now + timedelta(seconds=1)).isoformat(),
    )

    assert store.get_task(task.id).name == "提醒"
    assert len(store.list_tasks()) == 1

    claimed = store.claim_due_tasks(10, lock_seconds=60)
    assert claimed == []

    store.update_task(task.id, schedule_expr=(now - timedelta(seconds=1)).isoformat())
    claimed = store.claim_due_tasks(10, lock_seconds=60)
    assert [item.id for item in claimed] == [task.id]
    assert store.claim_due_tasks(10, lock_seconds=60) == []


def test_prepare_recurring_task_advances_before_completion(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="循环",
        prompt="执行",
        schedule_type="interval",
        schedule_expr="60",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    run = store.prepare_task_run(claimed)
    updated = store.get_task(task.id)

    assert run.status == "running"
    assert updated.running is True
    assert updated.enabled is True
    assert updated.next_run_at is not None
    assert updated.next_run_at > updated.last_run_at


def test_update_non_schedule_fields_preserves_next_run(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="原名",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="1h",
    )

    updated = store.update_task(task.id, name="新名")

    assert updated.name == "新名"
    assert updated.next_run_at == task.next_run_at


def test_skip_task_run_advances_recurring_task(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="循环",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    store.skip_task_run(claimed, error="busy")
    updated = store.get_task(task.id)

    assert updated.running is False
    assert updated.last_status == "skipped"
    assert updated.next_run_at is not None
    assert updated.next_run_at > datetime.now(timezone.utc)
    runs = store.list_runs(task.id)
    assert runs[0].status == "skipped"
    assert runs[0].error == "busy"


def test_once_task_is_consumed_on_scheduled_run(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="一次",
        prompt="hello",
        schedule_type="once",
        schedule_expr=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    claimed = store.claim_due_tasks(1, lock_seconds=60)[0]

    store.prepare_task_run(claimed)
    updated = store.get_task(task.id)

    assert updated.enabled is False
    assert updated.next_run_at is None


def test_claim_due_tasks_returns_actual_locked_rows(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="到期",
        prompt="hello",
        schedule_type="once",
        schedule_expr=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    claimed = store.claim_due_tasks(1, lock_seconds=60)

    assert len(claimed) == 1
    assert claimed[0].id == task.id
    assert claimed[0].running is True
    assert claimed[0].locked_until is not None


def test_preserved_once_run_keeps_future_schedule(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="一次",
        prompt="hello",
        schedule_type="once",
        schedule_expr=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    store.prepare_task_run(claimed, preserve_schedule=True)
    updated = store.get_task(task.id)

    assert updated.enabled is True
    assert updated.next_run_at == task.next_run_at


@pytest.mark.asyncio
async def test_runner_records_success(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="执行",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id):
            self.message = message
            self.role = role
            self.session_id = session_id
            return SimpleNamespace(output=f"done: {message}")

    service = Service()
    runner = ScheduledTaskRunner(store, service, default_timeout_seconds=10)
    await runner.run_task(claimed)

    runs = store.list_runs(task.id)
    updated = store.get_task(task.id)
    assert runs[0].status == "succeeded"
    assert runs[0].output == "done: hello"
    assert service.session_id == f"schedule-{task.id}"
    assert updated.running is False
    assert updated.last_status == "succeeded"


@pytest.mark.asyncio
async def test_scheduler_claims_due_task(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="到期",
        prompt="hello",
        schedule_type="once",
        schedule_expr=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id):
            return SimpleNamespace(output="ok")

    scheduler = TaskScheduler(
        store,
        Service(),
        poll_interval_seconds=60,
        max_concurrency=1,
        default_timeout_seconds=10,
        claim_lock_seconds=60,
    )
    stats = await scheduler.run_once()

    assert stats == {"claimed": 1, "started": 1}
    await asyncio.gather(*scheduler._active_tasks)
    assert store.list_runs(task.id)[0].status == "succeeded"
