# pyrefly: ignore [missing-import]
import pytest
import os
from pathlib import Path
from unittest.mock import MagicMock

from openhachimi_agent.core.config import AppConfig, MemoryConfig, ResearchConfig, SchedulerConfig, VisionConfig
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
        llm_supports_vision="auto",
        log_dir=tmp_path / ".logs",
        log_level="INFO",
        log_console=False,
        skills_dirs=[tmp_path / ".claude" / "skills"],
        browser_headless=True,
        browser_channel=None,
        browser_user_agent=None,
        browser_window_size=None,
        browser_idle_timeout=300,
        browser_cdp_wait_seconds=45,
        telegram_bot_token=None,
        telegram_proxy_url=None,
        show_tool_calls=True,
        attachments_dir=tmp_path / ".tmp" / "attachments",
        max_attachment_size_bytes=50 * 1024 * 1024,
        allowed_attachment_mime_types=[],
        agent_timeout_seconds=300,
        stream_idle_timeout_seconds=60,
        memory=MemoryConfig(db_path=memory_dir / "long_term_memory.sqlite3"),
        scheduler=SchedulerConfig(db_path=tmp_path / ".scheduler" / "tasks.sqlite3"),
        research=ResearchConfig(),
        vision=VisionConfig(api_key="test-key", base_url="http://test"),
        http_api_token="test-token",
    )
    return config

@pytest.fixture
def mock_browser_manager():
    bm = MagicMock()
    return bm

@pytest.fixture
def mock_agent_deps(mock_config, mock_browser_manager):
    # 跟 memory.db_path 现有用真实 SQLite 文件的套路对齐 —— SessionStore 自带 schema
    # 自举,tmp_path 范围内一次性建好,够整轮测试用。
    from openhachimi_agent.storage.session_store import SessionStore
    return AgentDeps(
        config=mock_config,
        session_id="test_session_123",
        browser_manager=mock_browser_manager,
        process_manager=MagicMock(),
        session_state={},
        session_store=SessionStore(mock_config.memory_dir / "sessions.sqlite3"),
    )
