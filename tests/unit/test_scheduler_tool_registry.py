from openhachimi_agent.tools.registry import EXECUTOR_TOOLSET, PLANNER_TOOLSET, SCHEDULED_EXECUTOR_TOOLSET


def _tool_names(toolset):
    tools = toolset.tools
    if isinstance(tools, dict):
        return set(tools.keys())
    return {getattr(tool, "__name__", "") or getattr(tool, "name", "") for tool in tools}


def test_executor_toolset_contains_scheduler_read_and_mutation_tools():
    names = _tool_names(EXECUTOR_TOOLSET)

    assert "list_scheduled_tasks" in names
    assert "read_schedule_inbox" in names
    assert "create_delayed_task" in names
    assert "create_scheduled_task" in names
    assert "pause_scheduled_task" in names
    assert "mark_schedule_run_read" in names
    assert "manage_scheduled_task" not in names


def test_scheduled_executor_toolset_contains_only_scheduler_read_tools():
    names = _tool_names(SCHEDULED_EXECUTOR_TOOLSET)

    assert "list_scheduled_tasks" in names
    assert "get_scheduled_task" in names
    assert "list_scheduled_task_runs" in names
    assert "read_schedule_inbox" in names
    assert "preview_scheduled_task_delivery" in names

    assert "create_delayed_task" not in names
    assert "create_scheduled_task" not in names
    assert "update_scheduled_task" not in names
    assert "update_scheduled_task_delivery" not in names
    assert "pause_scheduled_task" not in names
    assert "resume_scheduled_task" not in names
    assert "remove_scheduled_task" not in names
    assert "mark_schedule_run_read" not in names
    assert "manage_scheduled_task" not in names


def test_planner_toolset_does_not_expose_scheduler_mutation_tools():
    names = _tool_names(PLANNER_TOOLSET)

    assert "create_delayed_task" not in names
    assert "create_scheduled_task" not in names
    assert "update_scheduled_task" not in names
    assert "remove_scheduled_task" not in names
    assert "manage_scheduled_task" not in names
