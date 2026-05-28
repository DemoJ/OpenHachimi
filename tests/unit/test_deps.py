from openhachimi_agent.core.deps import AgentDeps

def test_agent_deps_properties(mock_config, mock_browser_manager):
    deps = AgentDeps(
        config=mock_config,
        session_id="test_session",
        browser_manager=mock_browser_manager
    )

    assert deps.session_id == "test_session"
    assert deps.config == mock_config
    assert deps.base_dir == mock_config.base_dir
    assert deps.skills_dirs == mock_config.skills_dirs
    assert deps.browser_manager is mock_browser_manager


def test_agent_deps_run_mode_default(mock_config):
    """测试 run_mode 默认为 interactive"""
    deps = AgentDeps(config=mock_config, session_id="test")
    assert deps.run_mode == "interactive"


def test_agent_deps_run_mode_scheduled(mock_config):
    """测试 run_mode 可以设置为 scheduled"""
    deps = AgentDeps(config=mock_config, session_id="test", run_mode="scheduled")
    assert deps.run_mode == "scheduled"


def test_agent_deps_channel_context_default(mock_config):
    """测试 channel_context 默认为空字典"""
    deps = AgentDeps(config=mock_config, session_id="test")
    assert deps.channel_context == {}


def test_agent_deps_channel_context_telegram(mock_config):
    """测试 Telegram 渠道上下文"""
    context = {
        "type": "telegram",
        "chat_id": 123456,
        "message_thread_id": 789
    }
    deps = AgentDeps(config=mock_config, session_id="test", channel_context=context)
    assert deps.channel_context == context


def test_agent_deps_channel_property_default(mock_config):
    """测试 channel 属性默认为 local"""
    deps = AgentDeps(config=mock_config, session_id="test")
    assert deps.channel == "local"


def test_agent_deps_channel_property_telegram(mock_config):
    """测试 channel 属性从 channel_context 读取"""
    deps = AgentDeps(
        config=mock_config,
        session_id="test",
        channel_context={"type": "telegram"}
    )
    assert deps.channel == "telegram"


def test_agent_deps_delivery_target_default(mock_config):
    """测试 delivery_target 默认为空字典"""
    deps = AgentDeps(config=mock_config, session_id="test")
    assert deps.delivery_target == {}


def test_agent_deps_delivery_target_from_channel_context(mock_config):
    """测试 delivery_target 从 channel_context 构建"""
    deps = AgentDeps(
        config=mock_config,
        session_id="test",
        channel_context={
            "type": "telegram",
            "platform": "telegram",
            "chat_id": 123456,
            "message_thread_id": 789
        }
    )
    expected = {
        "type": "telegram",
        "platform": "telegram",
        "chat_id": 123456,
        "message_thread_id": 789
    }
    assert deps.delivery_target == expected


def test_agent_deps_delivery_target_cli(mock_config):
    """测试 CLI 渠道的 delivery_target"""
    deps = AgentDeps(
        config=mock_config,
        session_id="test",
        channel_context={"type": "cli", "platform": "cli"}
    )
    assert deps.delivery_target == {"type": "cli", "platform": "cli"}


def test_agent_deps_delivery_target_http(mock_config):
    """测试 HTTP 渠道的 delivery_target（http 不在 platform 白名单内，返回空）"""
    deps = AgentDeps(
        config=mock_config,
        session_id="test",
        channel_context={"type": "http", "platform": "http"}
    )
    # http 不在 {"telegram", "cli", "inbox"} 白名单中
    assert deps.delivery_target == {}
