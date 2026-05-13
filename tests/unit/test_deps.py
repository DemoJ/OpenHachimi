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
