from openhachimi_agent.tools.registry import EXECUTOR_TOOLSET, PLANNER_TOOLSET, SCHEDULED_EXECUTOR_TOOLSET


def _tool_names(toolset):
    tools = toolset.tools
    if isinstance(tools, dict):
        return set(tools.keys())
    return {getattr(tool, "__name__", "") or getattr(tool, "name", "") for tool in tools}


def test_executor_toolset_contains_web_search():
    names = _tool_names(EXECUTOR_TOOLSET)

    assert "web_search" in names
    assert "browser_extract_content" in names
    # research_sources / research_next_queries 已合并删除,不应存在
    assert "research_sources" not in names
    assert "research_next_queries" not in names


def test_planner_toolset_does_not_contain_network_research_tools():
    names = _tool_names(PLANNER_TOOLSET)

    assert "web_search" not in names
    assert "web_fetch" not in names
    assert "browser_navigate" not in names
    assert "browser_extract_content" not in names


def test_executor_toolset_contains_install_skill_but_planner_does_not():
    executor_names = _tool_names(EXECUTOR_TOOLSET)
    scheduled_names = _tool_names(SCHEDULED_EXECUTOR_TOOLSET)
    planner_names = _tool_names(PLANNER_TOOLSET)

    assert "install_skill" in executor_names
    assert "install_skill" in scheduled_names
    assert "install_skill" not in planner_names
