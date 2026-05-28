"""定时任务 CLI 子命令测试。"""
import argparse
import sys
import pytest
from unittest.mock import patch, MagicMock

from openhachimi_agent.__main__ import cmd_schedule, _print_schedule


@pytest.fixture
def mock_request_json():
    with patch("openhachimi_agent.__main__.request_json") as mock:
        yield mock


@pytest.fixture
def mock_get_server_url():
    with patch("openhachimi_agent.__main__.get_server_url", return_value="http://127.0.0.1:8765") as mock:
        yield mock


@pytest.fixture
def mock_print(capsys):
    return capsys


def make_args(**kwargs):
    defaults = {
        "schedule_command": None,
        "name": None,
        "prompt": None,
        "once": None,
        "interval": None,
        "cron": None,
        "timezone": "UTC",
        "role": None,
        "session_id": None,
        "timeout": None,
        "paused": False,
        "delivery_mode": "origin",
        "all": False,
        "limit": 20,
        "mark_read": False,
        "id": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_schedule_add_with_interval(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = {
        "id": "abc123",
        "name": "test",
        "schedule_type": "interval",
        "schedule_expr": "60",
        "status": "enabled",
        "next_run_at": "2026-05-28T12:00:00Z",
        "role": None,
        "session_id": None,
        "delivery_mode": "origin",
    }
    args = make_args(schedule_command="add", name="test", prompt="hello", interval="60")
    cmd_schedule(args)

    mock_request_json.assert_called_once()
    call_args = mock_request_json.call_args
    assert call_args[0][1] == "POST"
    assert call_args[0][2] == "/schedules"
    payload = call_args[1]["payload"] if "payload" in call_args[1] else call_args[0][3]
    assert payload["name"] == "test"
    assert payload["prompt"] == "hello"
    assert payload["schedule_type"] == "interval"
    assert payload["schedule_expr"] == "60"


def test_schedule_add_with_once(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = {
        "id": "abc123",
        "name": "once-task",
        "schedule_type": "once",
        "schedule_expr": "2026-05-29T10:00:00+08:00",
        "status": "enabled",
        "next_run_at": "2026-05-29T10:00:00+08:00",
        "role": None,
        "session_id": None,
        "delivery_mode": "origin",
    }
    args = make_args(schedule_command="add", name="once-task", prompt="hello", once="2026-05-29T10:00:00+08:00")
    cmd_schedule(args)

    mock_request_json.assert_called_once()
    call_args = mock_request_json.call_args
    payload = call_args[0][3]
    assert payload["schedule_type"] == "once"


def test_schedule_add_with_cron(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = {
        "id": "abc123",
        "name": "cron-task",
        "schedule_type": "cron",
        "schedule_expr": "0 9 * * *",
        "status": "enabled",
        "next_run_at": "2026-05-29T09:00:00Z",
        "role": None,
        "session_id": None,
        "delivery_mode": "origin",
    }
    args = make_args(schedule_command="add", name="cron-task", prompt="hello", cron="0 9 * * *")
    cmd_schedule(args)

    mock_request_json.assert_called_once()
    call_args = mock_request_json.call_args
    payload = call_args[0][3]
    assert payload["schedule_type"] == "cron"


def test_schedule_add_no_schedule_type_exits(mock_request_json, mock_get_server_url, mock_print):
    args = make_args(schedule_command="add", name="test", prompt="hello")
    with pytest.raises(SystemExit):
        cmd_schedule(args)


def test_schedule_add_multiple_schedule_types_exits(mock_request_json, mock_get_server_url, mock_print):
    args = make_args(schedule_command="add", name="test", prompt="hello", interval="60", cron="0 9 * * *")
    with pytest.raises(SystemExit):
        cmd_schedule(args)


def test_schedule_add_paused(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = {
        "id": "abc123",
        "name": "paused-task",
        "schedule_type": "interval",
        "schedule_expr": "60",
        "status": "paused",
        "next_run_at": None,
        "role": None,
        "session_id": None,
        "delivery_mode": "origin",
    }
    args = make_args(schedule_command="add", name="paused-task", prompt="hello", interval="60", paused=True)
    cmd_schedule(args)

    call_args = mock_request_json.call_args
    payload = call_args[0][3]
    assert payload.get("status") == "paused"


def test_schedule_list(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = [
        {
            "id": "abc123",
            "name": "task1",
            "schedule_type": "interval",
            "schedule_expr": "60",
            "status": "enabled",
            "next_run_at": "2026-05-28T12:00:00Z",
            "role": None,
            "session_id": None,
            "delivery_mode": "origin",
            "running": False,
        }
    ]
    args = make_args(schedule_command="list")
    cmd_schedule(args)

    mock_request_json.assert_called_once()
    call_args = mock_request_json.call_args
    assert call_args[0][1] == "GET"
    assert "/schedules" in call_args[0][2]


def test_schedule_list_empty(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = []
    args = make_args(schedule_command="list")
    cmd_schedule(args)

    captured = mock_print.readouterr()
    assert "暂无" in captured.out


def test_schedule_list_with_all(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = []
    args = make_args(schedule_command="list", all=True)
    cmd_schedule(args)

    call_args = mock_request_json.call_args
    assert "include_deleted=true" in call_args[0][2]


def test_schedule_pause(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = {
        "id": "abc123",
        "name": "task1",
        "schedule_type": "interval",
        "schedule_expr": "60",
        "status": "paused",
        "next_run_at": None,
        "role": None,
        "session_id": None,
        "delivery_mode": "origin",
        "running": False,
    }
    args = make_args(schedule_command="pause", id="abc123")
    cmd_schedule(args)

    call_args = mock_request_json.call_args
    assert call_args[0][1] == "POST"
    assert "/schedules/abc123/pause" in call_args[0][2]


def test_schedule_resume(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = {
        "id": "abc123",
        "name": "task1",
        "schedule_type": "interval",
        "schedule_expr": "60",
        "status": "enabled",
        "next_run_at": "2026-05-28T12:00:00Z",
        "role": None,
        "session_id": None,
        "delivery_mode": "origin",
        "running": False,
    }
    args = make_args(schedule_command="resume", id="abc123")
    cmd_schedule(args)

    call_args = mock_request_json.call_args
    assert call_args[0][1] == "POST"
    assert "/schedules/abc123/resume" in call_args[0][2]


def test_schedule_remove(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = {"ok": True}
    args = make_args(schedule_command="remove", id="abc123")
    cmd_schedule(args)

    call_args = mock_request_json.call_args
    assert call_args[0][1] == "DELETE"
    assert "/schedules/abc123" in call_args[0][2]


def test_schedule_run(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = {
        "id": "run123",
        "status": "succeeded",
        "output": "done",
        "error": None,
    }
    args = make_args(schedule_command="run", id="abc123")
    cmd_schedule(args)

    call_args = mock_request_json.call_args
    assert call_args[0][1] == "POST"
    assert "/schedules/abc123/run" in call_args[0][2]

    captured = mock_print.readouterr()
    assert "run123" in captured.out


def test_schedule_inbox(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = [
        {
            "id": "run123",
            "status": "succeeded",
            "started_at": "2026-05-28T10:00:00Z",
            "delivery_status": "delivered",
            "output": "hello world",
            "error": None,
        }
    ]
    args = make_args(schedule_command="inbox")
    cmd_schedule(args)

    call_args = mock_request_json.call_args
    assert call_args[0][1] == "GET"
    assert "/schedules/inbox" in call_args[0][2]


def test_schedule_inbox_empty(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = []
    args = make_args(schedule_command="inbox")
    cmd_schedule(args)

    captured = mock_print.readouterr()
    assert "暂无" in captured.out


def test_schedule_inbox_with_mark_read(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = []
    args = make_args(schedule_command="inbox", mark_read=True)
    cmd_schedule(args)

    call_args = mock_request_json.call_args
    assert "mark_read=true" in call_args[0][2]


def test_schedule_logs(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = [
        {
            "id": "run123",
            "status": "succeeded",
            "started_at": "2026-05-28T10:00:00Z",
            "duration_ms": 1500,
            "output": "done",
            "error": None,
        }
    ]
    args = make_args(schedule_command="logs", id="abc123")
    cmd_schedule(args)

    call_args = mock_request_json.call_args
    assert call_args[0][1] == "GET"
    assert "/schedules/abc123/runs" in call_args[0][2]


def test_schedule_logs_empty(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.return_value = []
    args = make_args(schedule_command="logs", id="abc123")
    cmd_schedule(args)

    captured = mock_print.readouterr()
    assert "暂无" in captured.out


def test_print_schedule_enabled():
    task = {
        "id": "abc",
        "name": "test",
        "schedule_type": "interval",
        "schedule_expr": "60",
        "status": "enabled",
        "next_run_at": "2026-05-28T12:00:00Z",
        "role": "default",
        "session_id": "sess1",
        "delivery_mode": "origin",
        "running": False,
    }
    _print_schedule(task)


def test_print_schedule_paused():
    task = {
        "id": "abc",
        "name": "test",
        "schedule_type": "interval",
        "schedule_expr": "60",
        "status": "paused",
        "next_run_at": None,
        "role": None,
        "session_id": None,
        "delivery_mode": "inbox",
        "running": False,
    }
    _print_schedule(task)


def test_print_schedule_with_last_status():
    task = {
        "id": "abc",
        "name": "test",
        "schedule_type": "interval",
        "schedule_expr": "60",
        "status": "enabled",
        "next_run_at": "2026-05-28T12:00:00Z",
        "role": None,
        "session_id": None,
        "delivery_mode": "origin",
        "running": True,
        "last_status": "succeeded",
        "last_error": None,
        "last_delivery_status": "delivered",
        "last_delivery_error": None,
    }
    _print_schedule(task)


def test_schedule_network_error(mock_request_json, mock_get_server_url, mock_print):
    mock_request_json.side_effect = Exception("connection refused")
    args = make_args(schedule_command="list")
    with pytest.raises(SystemExit):
        cmd_schedule(args)
