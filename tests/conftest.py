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
    deps = AgentDeps(
        config=mock_config,
        session_id="test_session_123",
        browser_manager=mock_browser_manager,
        process_manager=MagicMock(),
        session_state={},
        session_store=SessionStore(mock_config.memory_dir / "sessions.sqlite3"),
    )
    yield deps
    # Python 3.13 对 sqlite3.Connection 的 __del__ 行为更严格:即使连接已被
    # contextmanager 正常关闭,GC 回收 MagicMock 时若内部 call_args_list 残存
    # Connection 对象引用也会触发 ResourceWarning。显式 gc.collect 确保在测试
    # 报告阶段之前完成回收,catch_warnings 兜底抑制 GC 期间的尾声噪音。
    import gc
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        del deps
        gc.collect()


@pytest.fixture(autouse=True)
def _reset_memory_store_cache():
    """每个测试后显式关闭并清空进程级 MemoryStore 缓存。

    ``get_memory_store`` 把实例缓存进模块级 ``_STORE_CACHE`` 跨测试存活,而
    ``MemoryStore`` 靠 ``threading.local`` 持有 SQLite 连接、仅在 ``__del__`` 关闭。
    Python 3.13 对含 ``__del__`` 对象的 GC 时机不稳定,残留连接在被回收前会触发
    ``ResourceWarning: unclosed database``。此夹具在测试体跑完后统一收口,保证
    连接被确定性关闭、缓存不跨用例污染。
    """
    yield
    from openhachimi_agent.memory.recall import close_memory_stores
    close_memory_stores()
