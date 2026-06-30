from openhachimi_agent.tools.registry import MAIN_TOOLSET


def _tool_names(toolset):
    tools = toolset.tools
    if isinstance(tools, dict):
        return set(tools.keys())
    return {getattr(tool, "__name__", "") or getattr(tool, "name", "") for tool in tools}


def test_main_toolset_contains_scheduler_read_and_mutation_tools():
    """主 agent 持全套工具(含 scheduler 写工具)。scheduled 模式下 scheduler 写工具
    的拦截由 ``ensure_scheduler_mutation_allowed`` 在 run_mode=scheduled 时负责,
    不再通过单独的 SCHEDULED_EXECUTOR_TOOLSET 裁剪。"""
    names = _tool_names(MAIN_TOOLSET)

    assert "list_scheduled_tasks" in names
    assert "read_schedule_inbox" in names
    assert "create_delayed_task" in names
    assert "create_scheduled_task" in names
    assert "pause_scheduled_task" in names
    assert "mark_schedule_run_read" in names
    assert "manage_scheduled_task" not in names
