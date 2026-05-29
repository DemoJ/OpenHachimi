from openhachimi_agent.tools.registry import EXECUTOR_TOOLSET, PLANNER_TOOLSET


def _tool_names(toolset):
    tools = toolset.tools
    if isinstance(tools, dict):
        return set(tools.keys())
    return {getattr(tool, "__name__", "") or getattr(tool, "name", "") for tool in tools}


def test_executor_toolset_contains_research_quality_tools():
    names = _tool_names(EXECUTOR_TOOLSET)

    assert "research_sources" in names
    assert "research_next_queries" in names
    assert "browser_extract_content" in names


def test_planner_toolset_does_not_contain_network_research_tools():
    names = _tool_names(PLANNER_TOOLSET)

    assert "web_search" not in names
    assert "web_fetch" not in names
    assert "research_sources" not in names
    assert "research_next_queries" not in names
    assert "browser_navigate" not in names
    assert "browser_extract_content" not in names
