import pytest

from openhachimi_agent.scheduler.delivery import (
    CliDeliverySender,
    DeliveryMessage,
    DeliverySenderRegistry,
    InboxDeliverySender,
    TelegramDeliverySender,
    deliver_scheduled_run,
)
from openhachimi_agent.scheduler.store import ScheduledTaskStore
from types import SimpleNamespace


def _completed_run(store, task, output="done"):
    claimed = store.claim_task_now(task.id, lock_seconds=60)
    run = store.prepare_task_run(claimed)
    store.complete_run(run.id, status="succeeded", output=output, duration_ms=1)
    completed = store.get_run(run.id)
    assert completed is not None
    return completed


def _make_registry(telegram_sender=None, cli_printer=None):
    registry = DeliverySenderRegistry()
    registry.register(InboxDeliverySender())
    if telegram_sender is not None:
        registry.register(TelegramDeliverySender(telegram_sender))
    if cli_printer is not None:
        registry.register(CliDeliverySender(cli_printer))
    return registry


def _fake_config():
    return SimpleNamespace(
        scheduler=SimpleNamespace(
            delivery=SimpleNamespace(home_targets=[], default_mode="origin", fallback_to_inbox=True),
        ),
    )


@pytest.mark.asyncio
async def test_deliver_scheduled_run_to_telegram(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="提醒",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        origin={"type": "telegram", "platform": "telegram", "chat_id": 123, "thread_id": 456},
        delivery_mode="origin",
    )
    run = _completed_run(store, task)
    sent = []

    async def sender(chat_id, text, thread_id):
        sent.append((chat_id, text, thread_id))

    registry = _make_registry(telegram_sender=sender)
    await deliver_scheduled_run(task, run, store=store, registry=registry, config=_fake_config())

    updated = store.get_run(run.id)
    assert sent == [(123, "[定时任务：提醒]\n\ndone", 456)]
    assert updated is not None
    assert updated.delivery_status == "delivered"
    assert updated.delivered_at is not None


@pytest.mark.asyncio
async def test_deliver_scheduled_run_missing_telegram_sender_fallback_inbox(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="提醒",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        origin={"type": "telegram", "platform": "telegram", "chat_id": 123},
        delivery_mode="origin",
        delivery_fallback={"enabled": True, "mode": "inbox", "targets": [{"type": "inbox", "box": "default"}]},
    )
    run = _completed_run(store, task)

    registry = _make_registry()
    await deliver_scheduled_run(task, run, store=store, registry=registry, config=_fake_config())

    updated = store.get_run(run.id)
    assert updated is not None
    assert updated.delivery_status == "fallback_delivered"
    assert store.list_inbox_runs()


@pytest.mark.asyncio
async def test_deliver_scheduled_run_to_cli_printer(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="提醒",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        origin={"type": "cli", "platform": "cli"},
        delivery_mode="origin",
    )
    run = _completed_run(store, task)
    printed = []

    registry = _make_registry(cli_printer=printed.append)
    await deliver_scheduled_run(task, run, store=store, registry=registry, config=_fake_config())

    updated = store.get_run(run.id)
    assert "[定时任务：提醒]" in printed[0]
    assert updated is not None
    assert updated.delivery_status == "delivered"


@pytest.mark.asyncio
async def test_deliver_scheduled_run_cli_without_printer_fallback_inbox(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="提醒",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        origin={"type": "cli", "platform": "cli"},
        delivery_mode="origin",
        delivery_fallback={"enabled": True, "mode": "inbox", "targets": [{"type": "inbox", "box": "default"}]},
    )
    run = _completed_run(store, task)

    registry = _make_registry()
    await deliver_scheduled_run(task, run, store=store, registry=registry, config=_fake_config())

    updated = store.get_run(run.id)
    assert updated is not None
    assert updated.delivery_status == "fallback_delivered"
    assert store.list_inbox_runs()


@pytest.mark.asyncio
async def test_deliver_inbox_mode(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="提醒",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        delivery_mode="inbox",
    )
    run = _completed_run(store, task)

    registry = _make_registry()
    await deliver_scheduled_run(task, run, store=store, registry=registry, config=_fake_config())

    updated = store.get_run(run.id)
    assert updated is not None
    assert updated.delivery_status == "delivered"
    assert store.list_inbox_runs()


@pytest.mark.asyncio
async def test_deliver_none_mode(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="提醒",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        delivery_mode="none",
    )
    run = _completed_run(store, task)

    registry = _make_registry()
    await deliver_scheduled_run(task, run, store=store, registry=registry, config=_fake_config())

    updated = store.get_run(run.id)
    assert updated is not None
    assert updated.delivery_status == "not_required"


@pytest.mark.asyncio
async def test_deliver_failed_run_message(tmp_path):
    store = ScheduledTaskStore(tmp_path / "tasks.sqlite3")
    task = store.create_task(
        name="失败任务",
        prompt="hello",
        schedule_type="interval",
        schedule_expr="60",
        delivery_mode="inbox",
    )
    claimed = store.claim_task_now(task.id, lock_seconds=60)
    run = store.prepare_task_run(claimed)
    store.complete_run(run.id, status="failed", error="boom", duration_ms=1)
    failed = store.get_run(run.id)
    assert failed is not None

    msg = DeliveryMessage(
        task_id=task.id,
        task_name=task.name,
        run_id=failed.id,
        status=failed.status,
        output=failed.output,
        error=failed.error,
        duration_ms=failed.duration_ms,
    )
    assert msg.format_text() == "[定时任务：失败任务] 执行失败：boom"


@pytest.mark.asyncio
async def test_delivery_sender_registry_unknown_type(tmp_path):
    registry = DeliverySenderRegistry()
    registry.register(InboxDeliverySender())

    msg = DeliveryMessage(
        task_id="t1",
        task_name="test",
        run_id="r1",
        status="succeeded",
        output="ok",
        error=None,
        duration_ms=1,
    )

    result = await registry.send({"type": "unknown"}, msg)
    assert result.status == "failed"
    assert "no sender" in result.error
