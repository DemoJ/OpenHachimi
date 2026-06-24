# pyrefly: ignore [missing-import]
import os
import pytest
import asyncio
from unittest.mock import MagicMock, patch

from openhachimi_agent.service import agent_service as agent_service_module
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.agent.intent import extract_urls
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.tools import mcp as mcp_module
import openhachimi_agent.service.browser  # Ensure module is loaded for mock.patch

@pytest.fixture
def agent_service(mock_config):
    # Mock browser manager initialization to avoid starting playwright in unit tests
    with patch("openhachimi_agent.service.browser.BrowserManager") as mock_bm:
        service = AgentService(mock_config)
        yield service

@pytest.mark.asyncio
async def test_session_lock_weakref(agent_service):
    session_id = "test_lock_session"
    
    # Get lock
    lock = agent_service._get_session_lock(session_id)
    assert lock is not None
    assert session_id in agent_service._session_locks
    
    # Delete local reference, forcing garbage collection
    del lock
    
    # WeakValueDictionary should automatically remove the lock
    assert session_id not in agent_service._session_locks

@pytest.mark.asyncio
async def test_stop_session(agent_service):
    session_id = "running_session"
    terminated: list[str] = []

    # Create a mock task
    async def dummy_task():
        await asyncio.sleep(1)

    task = asyncio.create_task(dummy_task())
    agent_service._running_tasks[session_id] = task
    agent_service.process_manager.terminate_session = lambda sid: (terminated.append(sid) or 1) if sid == session_id else 0

    # Call stop_session
    resp = await agent_service.stop_session(session_id)
    assert resp.message == "已成功中断当前任务。"
    assert not task.done() or task.cancelled()
    assert terminated == [session_id]
    with pytest.raises(asyncio.CancelledError):
        await task

    # Call stop_session when no task
    resp2 = await agent_service.stop_session("non_existent_session")
    assert resp2.message == "当前没有正在运行的任务。"


@pytest.mark.asyncio
async def test_stop_session_returns_after_issuing_interrupts_without_waiting_for_slow_cancel_cleanup(agent_service):
    session_id = "slow_cancel_session"
    terminated: list[str] = []

    async def slow_cancel_task():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(1)
            raise

    task = asyncio.create_task(slow_cancel_task())
    agent_service._running_tasks[session_id] = task
    agent_service.process_manager.terminate_session = lambda sid: (terminated.append(sid) or 1) if sid == session_id else 0

    resp = await asyncio.wait_for(agent_service.stop_session(session_id), timeout=0.2)

    assert resp.message == "已成功中断当前任务。"
    assert not task.done() or task.cancelled()
    assert terminated == [session_id]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_send_message_stop_bypasses_session_lock(agent_service):
    session_id = "locked_stop_session"

    async def dummy_task():
        await asyncio.sleep(1)

    task = asyncio.create_task(dummy_task())
    agent_service._running_tasks[session_id] = task
    lock = agent_service._get_session_lock(session_id)
    await lock.acquire()
    try:
        resp = await asyncio.wait_for(agent_service.send_message("/stop", "default", session_id), timeout=0.2)
    finally:
        lock.release()

    assert resp.output == "已成功中断当前任务。"
    assert resp.session_id == session_id
    assert not task.done() or task.cancelled()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_send_message_new_bypasses_session_lock_and_returns_new_session(agent_service):
    old_session_id = "locked_new_session"

    async def dummy_task():
        await asyncio.sleep(1)

    task = asyncio.create_task(dummy_task())
    agent_service._running_tasks[old_session_id] = task
    lock = agent_service._get_session_lock(old_session_id)
    await lock.acquire()
    try:
        resp = await asyncio.wait_for(agent_service.send_message("/new", "default", old_session_id), timeout=0.2)
    finally:
        lock.release()

    assert "新对话已准备好" in resp.output
    assert resp.session_id != old_session_id
    assert not task.done() or task.cancelled()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stream_events_stop_yields_control_event_without_lock(agent_service):
    session_id = "locked_stream_stop_session"

    async def dummy_task():
        await asyncio.sleep(1)

    task = asyncio.create_task(dummy_task())
    agent_service._running_tasks[session_id] = task
    lock = agent_service._get_session_lock(session_id)
    await lock.acquire()
    try:
        gen = agent_service.stream_events("/stop", "default", session_id)
        event = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
    finally:
        lock.release()

    assert event.type == "text"
    assert "已成功中断当前任务" in event.text
    assert not task.done() or task.cancelled()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_extract_requested_urls():
    assert extract_urls("请访问 https://example.com/a，然后总结") == ["https://example.com/a"]
    assert extract_urls("没有链接") == []


def test_agent_dependency_mtime_scans_role_and_skill_files_only(agent_service, mock_config, tmp_path):
    role_file = mock_config.roles_dir / "default.md"
    role_file.write_text("role", encoding="utf-8")
    skills_dir = mock_config.skills_dirs[0]
    skill_dir = skills_dir / "demo"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    ignored_file = skill_dir / "ignored.txt"
    skill_file.write_text("skill", encoding="utf-8")
    ignored_file.write_text("ignored", encoding="utf-8")

    role_mtime = 1000.0
    skill_mtime = 2000.0
    ignored_mtime = 3000.0
    os.utime(role_file, (role_mtime, role_mtime))
    os.utime(skill_file, (skill_mtime, skill_mtime))
    os.utime(ignored_file, (ignored_mtime, ignored_mtime))

    assert agent_service._get_agent_dependency_mtime("default") == skill_mtime


def test_agent_dependency_mtime_uses_short_ttl_cache(agent_service, mock_config):
    role_file = mock_config.roles_dir / "default.md"
    role_file.write_text("role", encoding="utf-8")
    os.utime(role_file, (1000.0, 1000.0))

    first_mtime = agent_service._get_agent_dependency_mtime("default")
    os.utime(role_file, (2000.0, 2000.0))

    assert agent_service._get_agent_dependency_mtime("default") == first_mtime

    checked_at, cached_mtime = agent_service._agent_dependency_mtime_cache
    agent_service._agent_dependency_mtime_cache = (
        checked_at - agent_service_module.AGENT_DEPENDENCY_MTIME_TTL_SECONDS - 1,
        cached_mtime,
    )

    assert agent_service._get_agent_dependency_mtime("default") == 2000.0


class FakeMCPToolset:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        if self.fail:
            raise RuntimeError(f"failed {self.name}")
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True


@pytest.mark.asyncio
async def test_reload_mcp_toolsets_keeps_only_connected_toolsets(agent_service, mock_config, monkeypatch):
    mock_config.user_dir.mkdir(parents=True)
    ok = FakeMCPToolset("ok")
    failed = FakeMCPToolset("failed", fail=True)

    monkeypatch.setattr(mcp_module, "load_mcp_toolsets", lambda config: [ok, failed])

    await agent_service.start()

    assert agent_service._mcp_toolsets == [ok]
    assert ok.entered is True
    assert failed.entered is False
    assert len(agent_service._mcp_errors) == 1
    assert "failed failed" in agent_service._mcp_errors[0]


@pytest.mark.asyncio
async def test_maybe_reload_mcp_toolsets_reloads_only_when_mcp_file_changes(agent_service, mock_config, monkeypatch):
    mock_config.user_dir.mkdir(parents=True)
    mcp_file = mock_config.user_dir / "mcp-servers.json"
    mcp_file.write_text('{"mcpServers": {}}', encoding="utf-8")
    first = FakeMCPToolset("first")
    second = FakeMCPToolset("second")
    calls = []

    def fake_load_mcp_toolsets(config):
        calls.append(config.mcp)
        return [first] if len(calls) == 1 else [second]

    monkeypatch.setattr(mcp_module, "load_mcp_toolsets", fake_load_mcp_toolsets)

    await agent_service.start()
    agent_service._agents["default:executor"] = (object(), 0.0)
    await agent_service._maybe_reload_mcp_toolsets()

    assert len(calls) == 1
    assert agent_service._mcp_toolsets == [first]
    assert agent_service._agents

    mcp_file.write_text('{"mcpServers": {"remote": {"url": "https://example.test/mcp"}}}', encoding="utf-8")
    await agent_service._maybe_reload_mcp_toolsets()

    assert len(calls) == 2
    assert agent_service._mcp_toolsets == [second]
    assert first.exited is True
    assert agent_service._agents == {}
