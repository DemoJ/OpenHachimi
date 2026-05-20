# pyrefly: ignore [missing-import]
import os
import pytest
import asyncio
from unittest.mock import MagicMock, patch

from openhachimi_agent.service import agent_service as agent_service_module
from openhachimi_agent.service.agent_service import AgentService
from openhachimi_agent.agent.intent import extract_urls
from openhachimi_agent.core.config import AppConfig
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
    
    # Create a mock task
    async def dummy_task():
        await asyncio.sleep(1)
        
    task = asyncio.create_task(dummy_task())
    agent_service._running_tasks[session_id] = task
    
    # Call stop_session
    resp = await agent_service.stop_session(session_id)
    assert resp.message == "已成功中断当前任务。"
    assert task.cancelled() or task.done()
    
    # Call stop_session when no task
    resp2 = await agent_service.stop_session("non_existent_session")
    assert resp2.message == "当前没有正在运行的任务。"


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
