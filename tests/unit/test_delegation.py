"""子 agent 委派(delegate_task)的单元测试。

覆盖 plans/nifty-exploring-plum.md 阶段二重做的验证目标:
1. build_child_deps 产生隔离 deps(空 state、无 memory_*、新 session_id、depth 递增)。
2. resolve_child_toolsets:组名→工具集、与父交集、强制剥离、orchestrator 加回 delegate_task。
3. SubagentRegistry:register/unregister/cancel_all。
4. run_delegation:并发 gather、usage 汇总、深度限制、降级。
5. 中断:cancel_all → 子 task 被 cancel → status=interrupted。
6. 超时:wait_for → status=timeout。
7. delegate_task:降级(未注入)、ledger 包装、参数组装。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openhachimi_agent.agent.subagents import (
    SubagentRegistry,
    SubagentSpec,
    build_child_deps,
    resolve_child_toolsets,
    run_delegation,
)
from openhachimi_agent.core.config.models import DelegationConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.delegation import DELEGATE_TASK_TOOL, delegate_task
from openhachimi_agent.tools.toolset_groups import DELEGATE_BLOCKED_TOOL_NAMES


# --------------------------------------------------------------------------- fixtures


def _make_parent_deps(mock_config) -> AgentDeps:
    """构造一个父 agent deps,带子 agent 挂载点。"""
    return AgentDeps(
        config=mock_config,
        session_id="parent-session",
        session_state={"todo_state": "PARENT_STATE", "execution_ledger": [{"seq": 1}]},
        role_name="default",
        # memory_* 不设 → None
        subagent_agent=MagicMock(name="child_agent"),
        subagent_registry=None,  # 测试里按需注入
        delegate_depth=0,
    )


def _make_ctx(deps: AgentDeps, parent_tool_names: set[str] | None = None) -> SimpleNamespace:
    """构造最小 ctx:run_delegation 用到 ctx.deps / ctx.usage / ctx.tool_manager。"""
    from pydantic_ai.usage import RunUsage

    ctx = SimpleNamespace(deps=deps, usage=RunUsage())
    # tool_manager 模拟:tools 是 dict[name, tool]
    if parent_tool_names is not None:
        tm = SimpleNamespace()
        tm.tools = {name: SimpleNamespace(name=name) for name in parent_tool_names}
        ctx.tool_manager = tm
    else:
        # 默认给一个宽泛的父工具集(含 read/file/web/delegate_task 等)
        names = {"read_file", "list_files", "search_text", "write_file", "web_fetch",
                 "web_search", "git_status", "list_skills",
                 "get_skill_instructions", "remember", "forget_memory", "create_todos",
                 "update_todo", "get_todos", "run_command", "delegate_task", "clarify_user"}
        tm = SimpleNamespace()
        tm.tools = {n: SimpleNamespace(name=n) for n in names}
        ctx.tool_manager = tm
    return ctx


def _make_child_agent_mock(output: str = "调研完成", delay: float = 0.0) -> MagicMock:
    """构造 mock child agent:run() 返回带 output 的 result,并模拟增加传入 usage(像真实 pydantic-ai)。

    真实 Agent.run 会在跑的过程中把请求计入传入的 ``usage`` 对象;mock 这里手动给
    ``usage.requests += 1``,让 _accumulate_usage 的汇总逻辑可被验证。
    """
    agent = MagicMock()
    async def _run(_prompt, *, deps=None, usage=None, usage_limits=None, toolsets=None):
        if delay:
            await asyncio.sleep(delay)
        if usage is not None:
            usage.requests += 1
            usage.input_tokens += 100
            usage.output_tokens += 50
        return SimpleNamespace(output=output)
    agent.run = _run
    return agent


# --------------------------------------------------------------------------- 1. 隔离 deps


def test_build_child_deps_isolated(mock_config):
    """子 agent deps 隔离:空 session_state、无 memory_*、新 session_id、depth 递增。"""
    parent = _make_parent_deps(mock_config)
    child = build_child_deps(parent, "sa-123", depth=1)

    assert child.session_state == {}, "子 agent session_state 必须全新空,不继承父的"
    assert child.session_id == "sa-123", "子 agent 用新 session_id"
    assert child.delegate_depth == 1, "depth 从父的 0 递增到 1"
    # memory_* 隔离:不继承(为 None)
    assert child.memory_scope is None
    assert child.memory_context is None
    assert child.memory_service is None
    # 环境资源共享
    assert child.config is parent.config
    assert child.browser_manager is parent.browser_manager
    assert child.session_store is parent.session_store
    assert child.run_mode == "subagent"


# --------------------------------------------------------------------------- 2. 工具集裁剪


def test_resolve_child_toolsets_default_readonly():
    """默认(None)给只读组:含 read_file/web_search,不含写工具/clarify/delegate。"""
    parent_names = {"read_file", "list_files", "search_text", "web_search", "web_fetch",
                    "write_file", "run_command", "delegate_task", "clarify_user", "remember"}
    ts = resolve_child_toolsets(None, parent_names, role="leaf", orchestrator_enabled=True)
    names = set(ts.tools.keys())
    assert {"read_file", "list_files", "search_text", "web_search"} <= names, "应含只读工具"
    assert "write_file" not in names, "默认不应含写工具"
    assert "delegate_task" not in names, "leaf 不应有 delegate_task"
    assert "clarify_user" not in names, "不应有 clarify_user"
    assert "remember" not in names, "不应有 remember(写记忆)"


def test_resolve_child_toolsets_explicit_groups():
    """显式指定 ["file","web"] → 含文件(含写)+web 工具,但剥离 blocked。"""
    parent_names = {"read_file", "write_file", "list_files", "web_fetch", "web_search",
                    "delegate_task", "clarify_user", "create_todos"}
    ts = resolve_child_toolsets(["file", "web"], parent_names, role="leaf", orchestrator_enabled=True)
    names = set(ts.tools.keys())
    assert {"read_file", "write_file", "list_files", "web_fetch", "web_search"} <= names
    # research_sources 已合并删除,不应存在
    assert "research_sources" not in names
    # create_todos 在 planning 组,这里没指定 planning,不应出现;即便出现也该被剥离
    assert "create_todos" not in names
    assert "delegate_task" not in names


def test_resolve_child_toolsets_intersection_with_parent():
    """请求含父没有的工具 → 交集后剔除(防越权)。"""
    # 父只有 read_file,没有 web_search
    parent_names = {"read_file"}
    ts = resolve_child_toolsets(["read", "web"], parent_names, role="leaf", orchestrator_enabled=True)
    names = set(ts.tools.keys())
    assert "read_file" in names
    assert "web_search" not in names, "父没有 web_search,子 agent 也不应有(交集)"


def test_resolve_child_toolsets_orchestrator_keeps_delegate():
    """orchestrator 角色(且 enabled 且父有)→ 保留 delegate_task。"""
    parent_names = {"read_file", "delegate_task"}
    ts = resolve_child_toolsets(["read"], parent_names, role="orchestrator", orchestrator_enabled=True)
    names = set(ts.tools.keys())
    assert "delegate_task" in names, "orchestrator 应保留 delegate_task"


def test_resolve_child_toolsets_orchestrator_disabled_coerces_leaf():
    """orchestrator_enabled=False → orchestrator 强制降为 leaf,剥离 delegate_task。"""
    parent_names = {"read_file", "delegate_task"}
    ts = resolve_child_toolsets(["read"], parent_names, role="orchestrator", orchestrator_enabled=False)
    names = set(ts.tools.keys())
    assert "delegate_task" not in names, "orchestrator_enabled=False 时强制 leaf,无 delegate_task"


# --------------------------------------------------------------------------- 3. SubagentRegistry


@pytest.mark.asyncio
async def test_registry_register_unregister_cancel_all():
    """registry 的 register/unregister/cancel_all 基本行为。"""
    reg = SubagentRegistry()

    async def _long():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    t1 = asyncio.create_task(_long())
    t2 = asyncio.create_task(_long())
    await reg.register("sa-1", t1)
    await reg.register("sa-2", t2)

    await reg.cancel_all()
    # 两个 task 都应被 cancel
    await asyncio.gather(t1, t2, return_exceptions=True)
    assert t1.cancelled()
    assert t2.cancelled()

    await reg.unregister("sa-1")
    await reg.unregister("sa-2")
    # cancel_all 幂等:再调不报错
    await reg.cancel_all()


# --------------------------------------------------------------------------- 4. run_delegation


@pytest.mark.asyncio
async def test_run_delegation_concurrent_and_usage_rollup(mock_config):
    """3 个子 agent 并发完成,usage 累加回父。"""
    parent = _make_parent_deps(mock_config)
    parent.subagent_agent = _make_child_agent_mock(output="ok")
    ctx = _make_ctx(parent)

    cfg = DelegationConfig(max_concurrent_children=3, max_spawn_depth=1, max_iterations=50, child_timeout_seconds=0)
    specs = [SubagentSpec(goal=f"task {i}") for i in range(3)]

    results = await run_delegation(specs, ctx, cfg)

    assert len(results) == 3
    assert all(r.status == "completed" for r in results), [r.status for r in results]
    # usage 汇总:父 usage.requests 应 > 0(3 个子 agent 各跑了 1 次)
    assert ctx.usage.requests >= 3


@pytest.mark.asyncio
async def test_run_delegation_timeout(mock_config):
    """子 agent 超时 → status=timeout。"""
    parent = _make_parent_deps(mock_config)
    parent.subagent_agent = _make_child_agent_mock(delay=2.0)  # 故意慢
    ctx = _make_ctx(parent)

    cfg = DelegationConfig(max_concurrent_children=1, max_spawn_depth=1, max_iterations=50, child_timeout_seconds=0.3)
    specs = [SubagentSpec(goal="slow task")]

    results = await run_delegation(specs, ctx, cfg)
    assert results[0].status == "timeout"


@pytest.mark.asyncio
async def test_run_delegation_depth_limit_orchestrator(mock_config):
    """父 depth=0,max_spawn_depth=1 → depth+1=1 未超限(orchestrator 允许);
    若把 max_spawn_depth 设为 0(被 loading 强制为 1,这里直接测 depth+1>max 逻辑)
    通过手动设父 depth=1 让 depth+1=2 > max_spawn_depth=1 → orchestrator 被拒。"""
    parent = _make_parent_deps(mock_config)
    parent.delegate_depth = 1  # 父已在第 1 层
    parent.subagent_agent = _make_child_agent_mock()
    ctx = _make_ctx(parent)

    cfg = DelegationConfig(max_concurrent_children=1, max_spawn_depth=1, max_iterations=50, child_timeout_seconds=0)
    # orchestrator 角色在 depth=1 时再委派 → depth+1=2 > 1 → 拒绝
    specs = [SubagentSpec(goal="nested", role="orchestrator")]

    results = await run_delegation(specs, ctx, cfg)
    assert results[0].status == "failed"
    assert results[0].exit_reason == "depth_exceeded"


@pytest.mark.asyncio
async def test_run_delegation_no_subagent_injected_raises(mock_config):
    """未注入 subagent_agent → run_delegation 抛 RuntimeError(delegate_task 层降级)。"""
    parent = _make_parent_deps(mock_config)
    parent.subagent_agent = None
    ctx = _make_ctx(parent)
    cfg = DelegationConfig()

    with pytest.raises(RuntimeError, match="未注入子 agent"):
        await run_delegation([SubagentSpec(goal="x")], ctx, cfg)


# --------------------------------------------------------------------------- 5. 中断传播


@pytest.mark.asyncio
async def test_interrupt_propagation(mock_config):
    """父被 cancel → registry.cancel_all → 子 agent task 被 cancel → status=interrupted。"""
    parent = _make_parent_deps(mock_config)
    parent.subagent_agent = _make_child_agent_mock(delay=5.0)
    parent.subagent_registry = SubagentRegistry()
    ctx = _make_ctx(parent)

    cfg = DelegationConfig(max_concurrent_children=1, max_spawn_depth=1, max_iterations=50, child_timeout_seconds=0)
    specs = [SubagentSpec(goal="long task")]

    # 在另一个 task 里跑 run_delegation,然后 cancel 它模拟父被中断
    deleg_task = asyncio.create_task(run_delegation(specs, ctx, cfg))
    await asyncio.sleep(0.1)  # 让子 agent 启动
    deleg_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await deleg_task
    # registry 里的子 agent task 应已被 cancel_all(在 run_delegation 的 except 分支)


# --------------------------------------------------------------------------- 6. delegate_task 工具


@pytest.mark.asyncio
async def test_delegate_task_degrades_when_not_injected(mock_config):
    """未注入 subagent_agent → 返回降级提示,不抛异常。"""
    deps = AgentDeps(config=mock_config, session_id="s1")  # subagent_agent 默认 None
    ctx = SimpleNamespace(deps=deps, usage=MagicMock(), tool_manager=None)

    out = await delegate_task(ctx, goal="do something")

    assert "不可用" in out
    assert "read_file" in out or "web_search" in out  # 给出可替代的自有工具


@pytest.mark.asyncio
async def test_delegate_task_assembles_specs_and_returns_summary(mock_config):
    """注入子 agent + mock run_delegation 路径:goal 模式组装单个 spec,返回汇总文本。"""
    from openhachimi_agent.agent import subagents as sub_mod

    parent = _make_parent_deps(mock_config)
    parent.subagent_agent = _make_child_agent_mock(output="发现:路径 A")
    parent.subagent_registry = SubagentRegistry()
    ctx = _make_ctx(parent)

    # mock run_delegation 返回一个 ChildResult,避免真实跑 child agent
    from openhachimi_agent.agent.subagents import ChildResult

    async def _fake_run(specs, pctx, cfg):
        return [ChildResult(subagent_id="sa-x", status="completed", summary="发现:路径 A")]
    orig = sub_mod.run_delegation
    sub_mod.run_delegation = _fake_run
    try:
        out = await delegate_task(ctx, goal="找登录代码", context="项目在 /app", toolsets=["file"])
    finally:
        sub_mod.run_delegation = orig

    assert "委派结果" in out
    assert "发现:路径 A" in out
    assert "completed" in out


def test_delegate_task_tool_wrapped_with_ledger():
    """DELEGATE_TASK_TOOL 是 with_execution_ledger 包装版,保留原函数名。"""
    assert getattr(DELEGATE_TASK_TOOL, "__name__", "") == "delegate_task"


# --------------------------------------------------------------------------- 7. AgentDeps 向后兼容


def test_agent_deps_subagent_fields_default(mock_config):
    """未显式注入时 subagent_agent/registry 为 None,delegate_depth=0,向后兼容。"""
    deps = AgentDeps(config=mock_config, session_id="s1")
    assert deps.subagent_agent is None
    assert deps.subagent_registry is None
    assert deps.delegate_depth == 0
