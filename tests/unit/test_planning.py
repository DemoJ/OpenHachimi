# pyrefly: ignore [missing-import]
import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass

from openhachimi_agent.tools.planning import (
    create_todos,
    get_todos,
    update_todo,
    _get_state,
    TodoState,
    with_execution_guard,
)

@dataclass
class MockRunContext:
    deps: MagicMock

@pytest.fixture
def mock_ctx(mock_agent_deps):
    return MockRunContext(deps=mock_agent_deps)



def test_create_and_get_todos(mock_ctx):
    tasks = ["Task 1", "Task 2"]
    res = create_todos(mock_ctx, tasks)
    assert "Task 1" in res
    assert "Task 2" in res
    assert "[ ] 1." in res
    assert "[ ] 2." in res


def test_create_todos_inherits_task_frame_contract(mock_ctx):
    mock_ctx.deps.session_state["task_frame"] = {
        "goal": "Visit the requested URL",
        "invariants": ["Do not replace target URL https://example.com/a"],
    }

    res = create_todos(mock_ctx, ["Open the page"])

    assert "目标：Visit the requested URL" in res
    assert "Do not replace target URL https://example.com/a" in res

def test_update_todo(mock_ctx):
    create_todos(mock_ctx, ["Task 1"])
    res = update_todo(mock_ctx, 1, "in-progress", "working on it")
    assert "[-]" in res
    assert "working on it" in res
    
    res2 = update_todo(mock_ctx, 1, "done", "finished")
    assert "[x]" in res2
    assert "finished" in res2


def test_create_todos_rejects_missing_dependency(mock_ctx):
    with pytest.raises(Exception, match="依赖不存在"):
        create_todos(mock_ctx, [
            {"id": 1, "description": "Task 1", "depends_on": [99]},
        ])


def test_create_todos_rejects_cycle(mock_ctx):
    with pytest.raises(Exception, match="循环依赖"):
        create_todos(mock_ctx, [
            {"id": 1, "description": "Task 1", "depends_on": [2]},
            {"id": 2, "description": "Task 2", "depends_on": [1]},
        ])


def test_update_todo_requires_dependencies_done(mock_ctx):
    create_todos(mock_ctx, [
        {"id": 1, "description": "Task 1"},
        {"id": 2, "description": "Task 2", "depends_on": [1]},
    ])

    res = update_todo(mock_ctx, 2, "in-progress")
    assert "依赖尚未完成" in res

    update_todo(mock_ctx, 1, "done", "finished")
    res2 = update_todo(mock_ctx, 2, "in-progress")
    assert "[-] 2." in res2


def test_all_done_deactivates_plan(mock_ctx):
    create_todos(mock_ctx, ["Task 1"])
    update_todo(mock_ctx, 1, "done", "finished")
    assert _get_state(mock_ctx).is_active is False


def test_execution_guard_requires_single_in_progress_task(mock_ctx):
    def mutating_tool(ctx):
        return {"ok": True}

    guarded = with_execution_guard(mutating_tool)
    create_todos(mock_ctx, ["Task 1"])

    with pytest.raises(Exception, match="必须先用 update_todo"):
        guarded(mock_ctx)

    update_todo(mock_ctx, 1, "in-progress")
    assert guarded(mock_ctx) == {"ok": True}


def test_execution_guard_respects_allowed_tools(mock_ctx):
    def write_file(ctx):
        return {"ok": True}

    guarded = with_execution_guard(write_file)
    create_todos(mock_ctx, [
        {"id": 1, "description": "Task 1", "allowed_tools": ["run_command"]},
    ])
    update_todo(mock_ctx, 1, "in-progress")

    with pytest.raises(Exception, match="未授权工具"):
        guarded(mock_ctx)
