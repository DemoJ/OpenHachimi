import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from openhachimi_agent.scheduler.models import ScheduleType
from openhachimi_agent.scheduler.runner import ScheduledTaskRunner, _build_scheduled_execution_prompt
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
    assert updated.status == "enabled"
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

    run = store.prepare_task_run(claimed)
    # After prepare, ONCE task is still enabled (not yet deleted)
    after_prepare = store.get_task(task.id)
    assert after_prepare.status == "enabled"
    assert after_prepare.next_run_at == task.next_run_at

    # After complete, ONCE task should be deleted
    store.complete_run(run.id, status="succeeded", output="done", duration_ms=100)
    updated = store.get_task(task.id)

    assert updated.status == "deleted"
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

    assert updated.status == "enabled"
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

        async def send_message(self, message, role, session_id, **kwargs):
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
    # v2: 定时任务 payload 不再 wrap [IMPORTANT:...] 前缀, send_message 直接收到 task.prompt
    assert runs[0].output == "done: hello"
    assert service.message == "hello"
    assert service.session_id == f"schedule-{task.id}"
    assert updated.running is False
    assert updated.last_status == "succeeded"


@pytest.mark.asyncio
async def test_runner_prefers_execution_context_session(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="执行",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        session_id="task-session",
        execution_policy={"session_id": "metadata-session"},
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            self.session_id = session_id
            return SimpleNamespace(output="ok")

    service = Service()
    runner = ScheduledTaskRunner(store, service, default_timeout_seconds=10)
    await runner.run_task(claimed)

    assert service.session_id == "metadata-session"


@pytest.mark.asyncio
async def test_runner_uses_isolated_session_by_default(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="执行",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        session_id="creator-session",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            self.session_id = session_id
            return SimpleNamespace(output="ok")

    service = Service()
    runner = ScheduledTaskRunner(store, service, default_timeout_seconds=10)
    await runner.run_task(claimed)

    assert service.session_id == f"schedule-{task.id}"


def test_build_scheduled_execution_prompt_returns_task_prompt_only(tmp_path):
    """v2: _build_scheduled_execution_prompt 只返回 task.prompt,不再 wrap [IMPORTANT:...]"""
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="更新团测系统提醒",
        prompt="提醒用户：⏰ 时间到！请记得更新团测系统。",
        schedule_type="once",
        schedule_expr=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
    )

    prompt = _build_scheduled_execution_prompt(task)

    assert "已经到期的定时任务" not in prompt
    assert "不是用户新发来的普通请求" not in prompt
    assert prompt == "提醒用户：⏰ 时间到！请记得更新团测系统。"


@pytest.mark.asyncio
async def test_runner_sends_task_prompt_only(tmp_path):
    """v2: runner 只发 task.prompt 作为 message, [IMPORTANT] 已在 system prompt(scheduled_executor.md) 中"""
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="更新团测系统提醒",
        prompt="提醒用户：⏰ 时间到！请记得更新团测系统。",
        schedule_type="interval",
        schedule_expr="60",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            self.message = message
            self.kwargs = kwargs
            return SimpleNamespace(output="ok")

    service = Service()
    runner = ScheduledTaskRunner(store, service, default_timeout_seconds=10)
    await runner.run_task(claimed)

    assert service.kwargs["run_mode"] == "scheduled"
    assert service.message == "提醒用户：⏰ 时间到！请记得更新团测系统。"


def test_store_tracks_delivery_and_inbox(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="提醒",
        prompt="hello",
        schedule_type="once",
        schedule_expr=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        delivery_mode="inbox",
        delivery_targets=[{"type": "inbox", "box": "default"}],
    )
    claimed = store.claim_due_tasks(1, lock_seconds=60)[0]
    run = store.prepare_task_run(claimed)
    store.complete_run(run.id, status="succeeded", output="done", duration_ms=1)
    store.update_run_delivery(run.id, delivery_status="delivered", delivery_targets=[{"type": "inbox", "box": "default"}])

    inbox = store.list_inbox_runs()
    assert len(inbox) == 1
    assert inbox[0][0].id == task.id
    assert inbox[0][1].delivery_status == "delivered"

    store.mark_run_read(run.id)
    assert store.list_inbox_runs(unread_only=True) == []


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

        async def send_message(self, message, role, session_id, **kwargs):
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


@pytest.mark.asyncio
async def test_scheduler_loop_survives_poll_exception(tmp_path, caplog):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            return SimpleNamespace(output="ok")

    scheduler = TaskScheduler(
        store,
        Service(),
        poll_interval_seconds=0,
        max_concurrency=1,
        default_timeout_seconds=10,
        claim_lock_seconds=60,
    )
    calls = 0

    async def flaky_run_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database is locked")
        scheduler.running = False
        return {"claimed": 0, "started": 0}

    scheduler.run_once = flaky_run_once
    scheduler.running = True

    with caplog.at_level("ERROR"):
        await asyncio.wait_for(scheduler._run_loop(), timeout=1)

    assert calls == 2
    assert "task scheduler poll failed" in caplog.text


def test_task_status_field(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="测试",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
    )

    assert task.status == "enabled"
    assert task.enabled is True

    paused = store.pause_task(task.id)
    assert paused.status == "paused"
    assert paused.enabled is False

    resumed = store.resume_task(task.id)
    assert resumed.status == "enabled"
    assert resumed.enabled is True

    deleted = store.delete_task(task.id)
    assert deleted.status == "deleted"
    assert deleted.enabled is False

    assert len(store.list_tasks()) == 0
    assert len(store.list_tasks(include_deleted=True)) == 1


def test_resolve_task_ref_by_name(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="唯一名称",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
    )

    resolved = store.resolve_task_ref("唯一名称")
    assert resolved is not None
    assert resolved.id == task.id

    resolved_by_id = store.resolve_task_ref(task.id)
    assert resolved_by_id is not None
    assert resolved_by_id.id == task.id


@pytest.mark.asyncio
async def test_runner_timeout(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="超时",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        timeout_seconds=1,
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            await asyncio.sleep(5)
            return SimpleNamespace(output="never")

    runner = ScheduledTaskRunner(store, Service(), default_timeout_seconds=1)
    run = await runner.run_task(claimed)

    assert run.status == "timeout"
    assert "超过" in run.error
    assert run.safety_status == "allowed"


@pytest.mark.asyncio
async def test_runner_exception(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="异常",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            raise RuntimeError("boom")

    runner = ScheduledTaskRunner(store, Service(), default_timeout_seconds=10)
    run = await runner.run_task(claimed)

    assert run.status == "failed"
    assert "boom" in run.error
    assert run.safety_status == "allowed"


@pytest.mark.asyncio
async def test_runner_session_already_running(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="重复",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    class Service:
        _running_tasks = {f"schedule-{task.id}": asyncio.Task(asyncio.sleep(10))}

        async def send_message(self, message, role, session_id, **kwargs):
            return SimpleNamespace(output="ok")

    runner = ScheduledTaskRunner(store, Service(), default_timeout_seconds=10)
    run = await runner.run_task(claimed)

    assert run.status == "skipped"
    assert "already running" in run.error

    Service._running_tasks[f"schedule-{task.id}"].cancel()


@pytest.mark.asyncio
async def test_runner_success_writes_safety_allowed(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="安全",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            return SimpleNamespace(output="ok")

    runner = ScheduledTaskRunner(store, Service(), default_timeout_seconds=10)
    run = await runner.run_task(claimed)

    assert run.status == "succeeded"
    assert run.safety_status == "allowed"
    assert run.execution_context.get("run_mode") == "scheduled"
    assert run.execution_context.get("task_id") == task.id


@pytest.mark.asyncio
async def test_scheduler_run_task_now(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="手动",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            return SimpleNamespace(output="ok")

    completed_items = []

    async def on_complete(t, r):
        completed_items.append((t.id, r.id))

    scheduler = TaskScheduler(
        store,
        Service(),
        poll_interval_seconds=60,
        max_concurrency=1,
        default_timeout_seconds=10,
        claim_lock_seconds=60,
        on_run_complete=on_complete,
    )
    run = await scheduler.run_task_now(claimed, preserve_schedule=True)

    assert run is not None
    assert run.status == "succeeded"
    assert len(completed_items) == 1
    assert completed_items[0] == (task.id, run.id)


@pytest.mark.asyncio
async def test_runner_builds_channel_context_from_origin(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="渠道",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        origin={"type": "telegram", "chat_id": 999, "platform": "telegram"},
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    captured = {}

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            captured["channel_context"] = kwargs.get("channel_context", {})
            captured["scheduler_context"] = kwargs.get("scheduler_context", {})
            captured["run_mode"] = kwargs.get("run_mode")
            return SimpleNamespace(output="ok")

    runner = ScheduledTaskRunner(store, Service(), default_timeout_seconds=10)
    run = await runner.run_task(claimed)

    assert run.status == "succeeded"
    assert captured["run_mode"] == "scheduled"
    assert captured["channel_context"]["type"] == "telegram"
    assert captured["channel_context"]["chat_id"] == 999
    assert captured["scheduler_context"]["task_id"] == task.id
    assert captured["scheduler_context"]["run_id"] == run.id


@pytest.mark.asyncio
async def test_runner_default_origin_fallback(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="无源",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)

    captured = {}

    class Service:
        _running_tasks = {}

        async def send_message(self, message, role, session_id, **kwargs):
            captured["channel_context"] = kwargs.get("channel_context", {})
            return SimpleNamespace(output="ok")

    runner = ScheduledTaskRunner(store, Service(), default_timeout_seconds=10)
    await runner.run_task(claimed)

    ctx = captured["channel_context"]
    assert ctx["type"] == "local"
    assert ctx["platform"] == "local"
