# pyrefly: ignore [missing-import]
import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass
from pydantic_ai.exceptions import ModelRetry

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


def test_execution_guard_allows_without_active_plan(mock_ctx):
    def mutating_tool(ctx):
        return {"ok": True}

    guarded = with_execution_guard(mutating_tool)

    assert guarded(mock_ctx) == {"ok": True}


def test_execution_guard_blocks_without_in_progress_task(mock_ctx):
    called = False

    def mutating_tool(ctx):
        nonlocal called
        called = True
        return {"ok": True}

    guarded = with_execution_guard(mutating_tool)
    create_todos(mock_ctx, ["Task 1"])

    with pytest.raises(ModelRetry, match="必须恰好有一个 in-progress"):
        guarded(mock_ctx)

    assert called is False

    update_todo(mock_ctx, 1, "in-progress")
    assert guarded(mock_ctx) == {"ok": True}


def test_execution_guard_blocks_non_authorized_tools(mock_ctx):
    called = False

    def write_file(ctx):
        nonlocal called
        called = True
        return {"ok": True}

    guarded = with_execution_guard(write_file)
    create_todos(mock_ctx, [
        {"id": 1, "description": "Task 1", "allowed_tools": ["run_command"]},
    ])
    update_todo(mock_ctx, 1, "in-progress")

    with pytest.raises(ModelRetry, match="未授权该工具"):
        guarded(mock_ctx)

    assert called is False


def test_execution_guard_allows_authorized_tool(mock_ctx):
    def write_file(ctx):
        return {"ok": True}

    guarded = with_execution_guard(write_file)
    create_todos(mock_ctx, [
        {"id": 1, "description": "Task 1", "allowed_tools": ["write_file"]},
    ])
    update_todo(mock_ctx, 1, "in-progress")

    assert guarded(mock_ctx) == {"ok": True}


def test_execution_guard_blocks_unfinished_dependencies(mock_ctx):
    called = False

    def mutating_tool(ctx):
        nonlocal called
        called = True
        return {"ok": True}

    guarded = with_execution_guard(mutating_tool)
    create_todos(mock_ctx, [
        {"id": 1, "description": "Prepare"},
        {"id": 2, "description": "Change", "depends_on": [1]},
    ])
    state = _get_state(mock_ctx)
    state.tasks[2].status = "in-progress"

    with pytest.raises(ModelRetry, match="依赖尚未完成"):
        guarded(mock_ctx)

    assert called is False


def test_update_todo_rejects_second_in_progress_task(mock_ctx):
    create_todos(mock_ctx, ["Task 1", "Task 2"])
    update_todo(mock_ctx, 1, "in-progress")

    res = update_todo(mock_ctx, 2, "in-progress")

    assert "已有任务正在进行" in res
    assert _get_state(mock_ctx).tasks[2].status == "pending"


def test_execution_guard_blocks_multiple_in_progress_tasks(mock_ctx):
    def mutating_tool(ctx):
        return {"ok": True}

    guarded = with_execution_guard(mutating_tool)
    create_todos(mock_ctx, ["Task 1", "Task 2"])
    state = _get_state(mock_ctx)
    state.tasks[1].status = "in-progress"
    state.tasks[2].status = "in-progress"

    with pytest.raises(ModelRetry, match="当前有 2 个"):
        guarded(mock_ctx)


@pytest.mark.asyncio
async def test_execution_guard_supports_async_tools(mock_ctx):
    async def run_command(ctx):
        return {"ok": True}

    guarded = with_execution_guard(run_command)
    create_todos(mock_ctx, [
        {"id": 1, "description": "Task 1", "allowed_tools": ["run_command"]},
    ])
    update_todo(mock_ctx, 1, "in-progress")

    assert await guarded(mock_ctx) == {"ok": True}


def test_update_todo_can_replace_allowed_tools(mock_ctx):
    """update_todo 支持修改 allowed_tools,让模型能在不重建计划的情况下修正白名单。"""
    def forget_memory(ctx):
        return "deleted"

    guarded = with_execution_guard(forget_memory)
    create_todos(mock_ctx, [
        {"id": 1, "description": "删除记忆", "allowed_tools": ["delete_path"]},
    ])
    update_todo(mock_ctx, 1, "in-progress")

    # 守卫拦截 forget_memory,因为只允许 delete_path
    with pytest.raises(ModelRetry, match="未授权该工具"):
        guarded(mock_ctx)

    # 用 update_todo 修正 allowed_tools
    update_todo(mock_ctx, 1, "in-progress", allowed_tools=["forget_memory"])
    state = _get_state(mock_ctx)
    assert state.tasks[1].allowed_tools == ["forget_memory"]

    # 守卫现在放行
    assert guarded(mock_ctx) == "deleted"


def test_update_todo_clear_allowed_tools_unrestricts(mock_ctx):
    """传 [] 给 allowed_tools 视为完全解除限制。"""
    def any_tool(ctx):
        return "ok"

    guarded = with_execution_guard(any_tool)
    create_todos(mock_ctx, [
        {"id": 1, "description": "Task", "allowed_tools": ["only_this"]},
    ])
    update_todo(mock_ctx, 1, "in-progress")
    update_todo(mock_ctx, 1, "in-progress", allowed_tools=[])

    state = _get_state(mock_ctx)
    assert state.tasks[1].allowed_tools == []
    assert guarded(mock_ctx) == "ok"


def test_update_todo_wildcard_allowed_tools_unrestricts(mock_ctx):
    """传 ['*'] 给 allowed_tools 也视为不限制。"""
    def any_tool(ctx):
        return "ok"

    guarded = with_execution_guard(any_tool)
    create_todos(mock_ctx, [
        {"id": 1, "description": "Task", "allowed_tools": ["nothing"]},
    ])
    update_todo(mock_ctx, 1, "in-progress")
    update_todo(mock_ctx, 1, "in-progress", allowed_tools=["*"])

    state = _get_state(mock_ctx)
    assert state.tasks[1].allowed_tools == ["*"]
    assert guarded(mock_ctx) == "ok"


def test_update_todo_omit_allowed_tools_keeps_existing(mock_ctx):
    """不传 allowed_tools 时不应改动原值。"""
    create_todos(mock_ctx, [
        {"id": 1, "description": "Task", "allowed_tools": ["a", "b"]},
    ])
    update_todo(mock_ctx, 1, "in-progress", notes="just a note")
    state = _get_state(mock_ctx)
    assert state.tasks[1].allowed_tools == ["a", "b"]
