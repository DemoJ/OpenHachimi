import pytest
import os
from pathlib import Path
from unittest.mock import MagicMock

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps

@pytest.fixture
def mock_config(tmp_path: Path):
    memory_dir = tmp_path / ".memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    
    config = AppConfig(
        base_dir=tmp_path,
        user_dir=tmp_path / "user",
        config_path=tmp_path / "user" / "config.yaml",
        roles_dir=roles_dir,
        memory_dir=memory_dir,
        model_name="test-model",
        openai_base_url="http://test",
        default_role_name="default",
        openai_api_key="test-key",
        log_dir=tmp_path / ".logs",
        log_level="INFO",
        log_console=False,
        skills_dirs=[tmp_path / ".claude" / "skills"],
        browser_headless=True,
        browser_channel=None,
        telegram_bot_token=None,
        telegram_proxy_url=None,
        agent_timeout_seconds=300,
        stream_idle_timeout_seconds=60
    )
    return config

@pytest.fixture
def mock_browser_manager():
    bm = MagicMock()
    return bm

@pytest.fixture
def mock_agent_deps(mock_config, mock_browser_manager):
    return AgentDeps(
        config=mock_config,
        session_id="test_session_123",
        browser_manager=mock_browser_manager
    )
