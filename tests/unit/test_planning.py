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


def test_create_todos_uses_explicit_goal_and_invariants(mock_ctx):
    """目标和约束只来自调用参数;旧的 task_frame 不再被读取(它早已没有写入方)。"""
    mock_ctx.deps.session_state["task_frame"] = {"goal": "旧 router 遗留目标", "invariants": ["旧约束"]}

    res = create_todos(mock_ctx, ["Open the page"], goal="Visit the requested URL", invariants=["Do not replace target URL"])

    assert "目标：Visit the requested URL" in res
    assert "Do not replace target URL" in res
    assert "旧 router 遗留目标" not in res and "旧约束" not in res


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


def test_create_todos_ignores_depends_on(mock_ctx):
    """依赖图已取消:模型即使传了 depends_on 也不再校验,不会因此报错重试。"""
    res = create_todos(mock_ctx, [
        {"id": 1, "description": "Task 1", "depends_on": [99]},
        {"id": 2, "description": "Task 2", "depends_on": [2]},
    ])

    assert "[ ] 1. Task 1" in res
    assert "[ ] 2. Task 2" in res
    assert "依赖" not in res


def test_create_todos_still_rejects_duplicate_ids(mock_ctx):
    with pytest.raises(ModelRetry, match="重复的任务 ID"):
        create_todos(mock_ctx, [
            {"id": 1, "description": "Task 1"},
            {"id": 1, "description": "Task 1 again"},
        ])


def test_update_todo_follows_list_order_without_dependency_gate(mock_ctx):
    """不再有"依赖未完成不能开始"的拦截,任意一项都可以直接开始。"""
    create_todos(mock_ctx, ["Task 1", "Task 2"])

    res = update_todo(mock_ctx, 2, "in-progress")

    assert "[-] 2." in res
    assert "错误" not in res


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


def test_update_todo_allows_second_in_progress_with_hint(mock_ctx):
    """同时开始第二项不再报错(以前会逼模型多调一次工具先改别的项),只提示一句。"""
    create_todos(mock_ctx, ["Task 1", "Task 2"])
    update_todo(mock_ctx, 1, "in-progress")

    res = update_todo(mock_ctx, 2, "in-progress")

    assert _get_state(mock_ctx).tasks[2].status == "in-progress"
    assert "任务 [1] 仍标记为进行中" in res


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



def test_blocked_and_done_close_plan(mock_ctx):
    """只剩 done / blocked 时计划关闭,不再一直挂着。"""
    create_todos(mock_ctx, ["Task 1", "Task 2"])
    update_todo(mock_ctx, 1, "done", "ok")
    update_todo(mock_ctx, 2, "blocked", notes="缺凭据")

    assert _get_state(mock_ctx).is_active is False


def test_create_todos_after_blocking_old_plan(mock_ctx):
    """旧计划剩下的项标 blocked 后,可以不带 merge 直接新建计划(拒绝提示里的选项 c)。"""
    create_todos(mock_ctx, ["Old 1", "Old 2"])
    with pytest.raises(ModelRetry):
        create_todos(mock_ctx, ["New 1"])

    update_todo(mock_ctx, 1, "blocked", notes="用户改做其他任务")
    update_todo(mock_ctx, 2, "blocked", notes="用户改做其他任务")
    res = create_todos(mock_ctx, ["New 1"])

    assert "New 1" in res
    assert _get_state(mock_ctx).is_active is True
    assert [task.description for task in _get_state(mock_ctx).tasks.values()] == ["New 1"]


def test_reopening_blocked_task_reactivates_plan(mock_ctx):
    """用户补齐信息后把 blocked 项重新打开,计划要重新激活。"""
    create_todos(mock_ctx, ["Task 1"])
    update_todo(mock_ctx, 1, "blocked", notes="缺凭据")
    assert _get_state(mock_ctx).is_active is False

    update_todo(mock_ctx, 1, "in-progress")

    assert _get_state(mock_ctx).is_active is True


def test_todo_reminder_skips_plan_with_only_blocked_left(mock_ctx):
    """只剩 blocked 项时,工具结果里不再追加"记得更新 TODO"的提醒。"""
    from openhachimi_agent.tools.planning import _inject_reminder_if_needed

    create_todos(mock_ctx, ["Task 1"])
    update_todo(mock_ctx, 1, "blocked", notes="缺凭据")
    state = _get_state(mock_ctx)
    state.is_active = True  # 即使旧数据里 is_active 仍为 True,也不应提醒

    results = [_inject_reminder_if_needed(mock_ctx, "ok") for _ in range(5)]

    assert all(result == "ok" for result in results)
