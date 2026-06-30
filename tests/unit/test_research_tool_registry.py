from openhachimi_agent.tools.registry import MAIN_TOOLSET


def _tool_names(toolset):
    tools = toolset.tools
    if isinstance(tools, dict):
        return set(tools.keys())
    return {getattr(tool, "__name__", "") or getattr(tool, "name", "") for tool in tools}


def test_main_toolset_contains_web_search():
    names = _tool_names(MAIN_TOOLSET)

    assert "web_search" in names
    assert "browser_extract_content" in names
    # research_sources / research_next_queries 已合并删除,不应存在
    assert "research_sources" not in names
    assert "research_next_queries" not in names


def test_main_toolset_contains_install_skill():
    names = _tool_names(MAIN_TOOLSET)
    assert "install_skill" in names


def test_main_toolset_contains_create_todos_as_normal_tool():
    """Hermes 式重构后 create_todos 是普通工具(不再是 planner output tool),
    与 get_todos / update_todo 一起在 MAIN_TOOLSET。"""
    names = _tool_names(MAIN_TOOLSET)
    assert "create_todos" in names
    assert "get_todos" in names
    assert "update_todo" in names
