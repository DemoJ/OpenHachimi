# pyrefly: ignore [missing-import]
import pytest
import asyncio
from unittest.mock import MagicMock, patch

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
