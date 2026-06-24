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


def test_update_todo_accepts_string_task_id(mock_ctx):
    """GLM / Qwen 等开源模型常生成 task_id="1" 而非 1。
    update_todo 必须吞掉这种格式差异,不能让 pydantic_ai 的 schema 校验抛
    ValidationError,否则连续 3 次累计就会撞 max_retries 把整轮报废。"""
    create_todos(mock_ctx, ["Task 1"])
    res = update_todo(mock_ctx, "1", "done", "ok")
    assert "[x] 1." in res


def test_update_todo_accepts_status_aliases(mock_ctx):
    """模型经常使用 "in_progress" / "completed" / "finished" 等同义写法,
    必须归一化到 Literal 内的 4 个值,避免反复 retry。"""
    create_todos(mock_ctx, ["Task A", "Task B", "Task C"])

    res_in_progress = update_todo(mock_ctx, 1, "in_progress")
    assert "[-] 1." in res_in_progress

    res_done = update_todo(mock_ctx, 1, "completed", "ok")
    assert "[x] 1." in res_done

    res_blocked = update_todo(mock_ctx, 2, "cancelled", notes="external dep missing")
    assert "external dep missing" in res_blocked


def test_update_todo_rejects_unknown_status_softly(mock_ctx):
    """无法识别的 status 也只返回错误字符串,绝不抛异常——
    抛异常会被 pydantic_ai 计入 retries,造成 max_retries 熔断。"""
    create_todos(mock_ctx, ["Task 1"])
    res = update_todo(mock_ctx, 1, "nonsense-status")
    assert "未识别" in res


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
        {"id": 1, "description": "Task 1"},
    ])
    update_todo(mock_ctx, 1, "in-progress")

    assert await guarded(mock_ctx) == {"ok": True}


def test_create_todos_cross_turn_blocks_without_merge(mock_ctx):
    """跨轮再次 create_todos 时,若未传 merge=True,应拒绝覆盖既有活动计划。"""
    mock_ctx.deps.session_state["current_turn_ledger_start_seq"] = 1
    create_todos(mock_ctx, ["Task A"])
    update_todo(mock_ctx, 1, "in-progress")

    # 模拟跨轮:新一轮 ledger seq 推进
    mock_ctx.deps.session_state["current_turn_ledger_start_seq"] = 10

    with pytest.raises(ModelRetry, match="跨轮活动计划"):
        create_todos(mock_ctx, ["Brand new task"])

    state = _get_state(mock_ctx)
    assert state.tasks[1].description == "Task A"
    assert state.tasks[1].status == "in-progress"


def test_create_todos_cross_turn_merge_keeps_status(mock_ctx):
    """跨轮 merge=True 时应按 id 合并:更新 description 等可变字段,
    保留旧任务的 status/evidence/notes。"""
    mock_ctx.deps.session_state["current_turn_ledger_start_seq"] = 1
    create_todos(mock_ctx, [
        {"id": 1, "description": "Original"},
        {"id": 2, "description": "Will stay"},
    ])
    update_todo(mock_ctx, 1, "in-progress", notes="halfway", evidence="ev")

    mock_ctx.deps.session_state["current_turn_ledger_start_seq"] = 10
    res = create_todos(
        mock_ctx,
        [
            {"id": 1, "description": "Refined"},
            {"id": 3, "description": "New task"},
        ],
        merge=True,
    )

    state = _get_state(mock_ctx)
    assert state.tasks[1].description == "Refined"
    assert state.tasks[1].status == "in-progress"
    assert state.tasks[1].notes == "halfway"
    assert state.tasks[1].evidence == "ev"
    # 未在新列表的旧 id 应保留
    assert 2 in state.tasks
    assert state.tasks[2].description == "Will stay"
    # 新 id 应追加
    assert state.tasks[3].description == "New task"
    assert "Refined" in res


def test_create_todos_same_turn_replace_allowed(mock_ctx):
    """同一轮内重复 create_todos 视作 planner refine,允许全量替换。"""
    mock_ctx.deps.session_state["current_turn_ledger_start_seq"] = 5
    create_todos(mock_ctx, ["Old A", "Old B"])
    # 同一轮再调一次,无 merge 也应允许覆盖
    res = create_todos(mock_ctx, ["Fresh"])

    state = _get_state(mock_ctx)
    assert len(state.tasks) == 1
    assert state.tasks[1].description == "Fresh"
    assert "Fresh" in res

