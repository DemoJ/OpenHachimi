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


def test_execution_guard_removed_allows_mutating_without_in_progress(mock_ctx):
    """execution_guard 已随 Hermes 式重构拆除:变动工具不再被"必须有恰好一个
    in-progress 任务"硬拦截。主 agent 靠 prompt 软引导 + verification 停止闸门
    兜底。这里只断言旧 guard 语义已不复存在——没有计划时直接调用不抛。"""
    def mutating_tool(ctx):
        return {"ok": True}

    # 不再包 guard;直接调用应放行
    assert mutating_tool(mock_ctx) == {"ok": True}
    create_todos(mock_ctx, ["Task 1"])
    # 有计划但无 in-progress,旧 guard 会拦;现在也不拦
    assert mutating_tool(mock_ctx) == {"ok": True}


def test_update_todo_rejects_second_in_progress_task(mock_ctx):
    create_todos(mock_ctx, ["Task 1", "Task 2"])
    update_todo(mock_ctx, 1, "in-progress")

    res = update_todo(mock_ctx, 2, "in-progress")

    assert "已有任务正在进行" in res
    assert _get_state(mock_ctx).tasks[2].status == "pending"


def test_create_todos_cross_turn_blocks_without_merge(mock_ctx):
    """已有活动计划时,再次 create_todos 若未传 merge=True,应拒绝覆盖。"""
    create_todos(mock_ctx, ["Task A"])
    update_todo(mock_ctx, 1, "in-progress")

    with pytest.raises(ModelRetry, match="活动计划"):
        create_todos(mock_ctx, ["Brand new task"])

    state = _get_state(mock_ctx)
    assert state.tasks[1].description == "Task A"
    assert state.tasks[1].status == "in-progress"


def test_create_todos_cross_turn_merge_keeps_status(mock_ctx):
    """merge=True 时应按 id 合并:更新 description 等可变字段,
    保留旧任务的 status/evidence/notes。"""
    create_todos(mock_ctx, [
        {"id": 1, "description": "Original"},
        {"id": 2, "description": "Will stay"},
    ])
    update_todo(mock_ctx, 1, "in-progress", notes="halfway", evidence="ev")

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


def test_create_todos_same_turn_still_requires_merge(mock_ctx):
    """拆除 planner 后不再有"同轮 refine"豁免:一旦存在活动计划,同轮内再次
    create_todos(无 merge)同样被拒——主 agent 要修订计划就走 merge=True。
    这避免了"中途 create_todos 全量替换"造成的突兀抖动。"""
    create_todos(mock_ctx, ["Old A", "Old B"])
    with pytest.raises(ModelRetry, match="活动计划"):
        create_todos(mock_ctx, ["Fresh"])

    state = _get_state(mock_ctx)
    assert len(state.tasks) == 2
    assert state.tasks[1].description == "Old A"

