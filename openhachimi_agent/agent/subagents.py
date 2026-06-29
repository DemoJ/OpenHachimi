"""子 agent 委派的核心编排(pydantic-ai 原生方式实现 hermes 委派逻辑)。

设计对齐 hermes ``delegate_task``(见 .tmp/hermes-agent-ref/tools/delegate_tool.py),
但用 pydantic-ai 原生 async 能力替代 hermes 的 ThreadPool / flag 轮询:

| hermes 手段                  | 本模块的 pydantic-ai 原生实现                       |
|-----------------------------|---------------------------------------------------|
| 重建 AIAgent 换 toolset      | 复用同一 Agent 实例,child.run(..., toolsets=[...]) |
| ThreadPoolExecutor 并发      | asyncio.gather(Agent.run 本身 async)              |
| _interrupt_requested 轮询    | asyncio.create_task + task.cancel + CancelledError |
| iteration_budget=None 独立   | child.run(..., usage=RunUsage()) 独立,事后累加回父 |
| skip_memory=True 全新会话    | 构造隔离 AgentDeps(空 state、不传 memory_*)        |

隔离哲学(照搬 hermes):子 agent 全新会话、零长期记忆、独立预算、不继承父 history。
父 agent 必须把所需信息通过 delegate_task 的 ``context`` 参数显式传递——这是
"subagents know nothing" 原则,也是委派的价值所在(隔离)。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.usage import RunUsage, UsageLimits

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.toolset_groups import (
    DELEGATE_BLOCKED_TOOL_NAMES,
    TOOLSET_GROUPS,
    group_tool_names,
)


logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────

@dataclass
class SubagentSpec:
    """单个子 agent 委派任务描述(对应 hermes delegate_task 的单条 task)。"""

    goal: str
    context: str = ""
    toolsets: list[str] | None = None   # 组名列表,如 ["file","web"];None=默认只读
    role: str = "leaf"                   # leaf | orchestrator
    max_iterations: int | None = None


@dataclass
class ChildResult:
    """单个子 agent 的运行结果(对应 hermes _run_single_child 返回的 entry)。"""

    subagent_id: str
    status: str          # completed | interrupted | timeout | failed
    summary: str         # 子 agent 的最终文本输出(self-report)
    exit_reason: str = ""
    usage: RunUsage = field(default_factory=RunUsage)


class SubagentRegistry:
    """运行中子 agent task 的注册表,供中断传播。

    父 agent 被 cancel 时,``run_delegation`` 的 finally 会调 :meth:`cancel_all`
    中断所有未完成的子 agent task——无需在 turn.py 的 /stop 路径单独接入
    (CancelledError 自然传播到 await gather 处,finally 兜底清理)。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def register(self, subagent_id: str, task: asyncio.Task) -> None:
        async with self._lock:
            self._tasks[subagent_id] = task

    async def unregister(self, subagent_id: str) -> None:
        async with self._lock:
            self._tasks.pop(subagent_id, None)

    async def cancel_all(self) -> None:
        """中断所有运行中的子 agent task。幂等。"""
        async with self._lock:
            tasks = list(self._tasks.values())
        for t in tasks:
            if not t.done():
                t.cancel()


# ── 隔离 deps 构造 ─────────────────────────────────────────────────────────

def build_child_deps(parent_deps: AgentDeps, subagent_id: str, depth: int) -> AgentDeps:
    """构造隔离的子 agent deps(照搬 hermes skip_memory / 全新会话哲学)。

    共享的(环境资源,隔离无意义):config / browser_manager / process_manager /
    session_store(用新 session_id 写入,自然隔离)/ role_name(决定可见 skills)。
    隔离的:session_state(全新空)、memory_*(不传→子 agent 不读不写长期记忆)、
    session_id(全新)、delegate_depth(父 depth+1)。
    """
    return AgentDeps(
        config=parent_deps.config,
        session_id=subagent_id,
        browser_manager=parent_deps.browser_manager,
        process_manager=parent_deps.process_manager,
        session_state={},                       # 全新空,不继承父的 todo_state/ledger
        # memory_scope / memory_context / memory_service 不传 → 子 agent 工具里
        # search_memory 在缺 memory_service 时优雅返回空(等价 hermes skip_memory=True)
        session_store=parent_deps.session_store,  # 共享 store,但用新 session_id 写入隔离
        run_mode="subagent",
        role_name=parent_deps.role_name,
        subagent_agent=parent_deps.subagent_agent,  # orchestrator 可再委派(受 depth 限制)
        subagent_registry=parent_deps.subagent_registry,
        delegate_depth=depth,
    )


# ── 工具集裁剪 ─────────────────────────────────────────────────────────────

def resolve_child_toolsets(
    requested_groups: list[str] | None,
    parent_tool_names: set[str],
    role: str,
    orchestrator_enabled: bool,
) -> FunctionToolset:
    """把组名解析成子 agent 可用的工具集(交集 + 强制剥离,对齐 hermes)。

    1. 组名展开:requested_groups=None 时默认只读组 ["read","web","git","skills"]。
    2. 与父工具集做交集:只允许父 agent 自己也有的工具(防越权)。
    3. 强制剥离 DELEGATE_BLOCKED_TOOL_NAMES(leaf 物理上无法委派/交互/写记忆/改计划)。
    4. orchestrator 角色(且 orchestrator_enabled 且 depth 允许)时,把 delegate_task
       加回——使其能再委派孙子 agent(受 max_spawn_depth 限制,在 run_delegation 入口检查)。
    """
    # 1. 默认只读组
    groups = requested_groups if requested_groups else ["read", "web", "git", "skills"]
    wanted = group_tool_names(groups)

    # 2. 与父工具集做交集(防越权)
    allowed = {name for name in wanted if name in parent_tool_names}

    # 3. 强制剥离
    allowed -= DELEGATE_BLOCKED_TOOL_NAMES

    # 4. orchestrator 加回 delegate_task
    effective_role = role
    if role == "orchestrator" and not orchestrator_enabled:
        effective_role = "leaf"
    if effective_role == "orchestrator" and "delegate_task" in parent_tool_names:
        allowed.add("delegate_task")

    # 收集工具函数(从 TOOLSET_GROUPS 反查,去重保序)
    funcs: list = []
    seen: set[str] = set()
    for g in groups:
        for func in TOOLSET_GROUPS.get(g, []):
            name = getattr(func, "__name__", "")
            if name in allowed and name not in seen:
                seen.add(name)
                funcs.append(func)

    # orchestrator 角色时,把 delegate_task 工具加回(使其能再委派孙子 agent)。
    # 延迟导入打破 subagents ↔ delegation 循环(delegation.py 顶层不导入本模块)。
    if effective_role == "orchestrator" and "delegate_task" in allowed:
        from openhachimi_agent.tools.delegation import DELEGATE_TASK_TOOL
        if "delegate_task" not in seen:
            funcs.append(DELEGATE_TASK_TOOL)

    return FunctionToolset(tools=funcs)


# ── 单个子 agent 运行 ──────────────────────────────────────────────────────

async def _run_one_child(
    spec: SubagentSpec,
    child_agent: Any,
    parent_ctx: RunContext[AgentDeps],
    registry: SubagentRegistry,
    parent_tool_names: set[str],
    cfg: Any,
) -> ChildResult:
    """跑单个子 agent,处理超时/中断,返回 ChildResult。

    - 独立 RunUsage(独立预算),事后由 run_delegation 累加回父 ctx.usage。
    - asyncio.wait_for 实现挂钟超时(cfg.child_timeout_seconds>0 时)。
    - CancelledError(父被中断传播)→ status="interrupted"。
    """
    subagent_id = f"sa-{uuid.uuid4().hex[:8]}"
    depth = parent_ctx.deps.delegate_depth + 1
    child_deps = build_child_deps(parent_ctx.deps, subagent_id, depth)

    child_toolset = resolve_child_toolsets(
        spec.toolsets, parent_tool_names, spec.role, cfg.orchestrator_enabled,
    )

    # 组装子 agent 的 user prompt:goal + context(对齐 hermes _build_child_system_prompt)
    prompt_parts = [f"YOUR TASK:\n{spec.goal}"]
    if spec.context:
        prompt_parts.append(f"CONTEXT:\n{spec.context}")
    user_prompt = "\n\n".join(prompt_parts)

    child_usage = RunUsage()
    usage_limits = UsageLimits(
        request_limit=spec.max_iterations or cfg.max_iterations,
    )

    logger.info(
        "spawning subagent %s (depth=%d, role=%s, toolsets=%s) goal=%s",
        subagent_id, depth, spec.role, spec.toolsets, spec.goal[:120],
    )

    # 运行时把裁剪后的 toolset 传入(替换 agent 构建时的骨架 toolset)
    run_coro = child_agent.run(
        user_prompt,
        deps=child_deps,
        usage=child_usage,
        usage_limits=usage_limits,
        toolsets=[child_toolset],
    )

    # 包成 task 注册到 registry(供中断传播),再套 wait_for(超时)
    task = asyncio.create_task(run_coro, name=subagent_id)
    await registry.register(subagent_id, task)

    timeout = cfg.child_timeout_seconds
    try:
        if timeout and timeout > 0:
            result = await asyncio.wait_for(task, timeout=timeout)
        else:
            result = await task
        return ChildResult(
            subagent_id=subagent_id,
            status="completed",
            summary=str(result.output) if result.output is not None else "",
            exit_reason="completed",
            usage=child_usage,
        )
    except asyncio.TimeoutError:
        return ChildResult(
            subagent_id=subagent_id,
            status="timeout",
            summary=f"[子 agent 超时({timeout}s),被中断]",
            exit_reason="timeout",
            usage=child_usage,
        )
    except asyncio.CancelledError:
        # 父被中断 → 传播到子;吞掉 CancelledError,返回 interrupted(不让父的 gather 整体崩)
        return ChildResult(
            subagent_id=subagent_id,
            status="interrupted",
            summary="[子 agent 因父 agent 中断而取消]",
            exit_reason="interrupted",
            usage=child_usage,
        )
    except Exception as exc:
        logger.warning("subagent %s failed: %s", subagent_id, exc, exc_info=True)
        return ChildResult(
            subagent_id=subagent_id,
            status="failed",
            summary=f"[子 agent 运行失败: {exc}]",
            exit_reason="failed",
            usage=child_usage,
        )
    finally:
        await registry.unregister(subagent_id)


# ── 并发编排 ───────────────────────────────────────────────────────────────

async def run_delegation(
    specs: list[SubagentSpec],
    parent_ctx: RunContext[AgentDeps],
    cfg: Any,
) -> list[ChildResult]:
    """并发跑多个子 agent,汇总 usage 回父,深度检查。

    - 深度检查:父 depth+1 超 max_spawn_depth → 该 spec 直接返回 error,不启动。
    - 并发:asyncio.gather(return_exceptions=True),上限 cfg.max_concurrent_children
      (用 semaphore 限流,超额并发不报错而是排队——比 hermes 的报错更友好)。
    - 中断:父被 cancel 时 gather 抛 CancelledError,finally 里 registry.cancel_all()
      兜底中断所有未完成子 agent。
    - usage 汇总:各子 agent 独立 RunUsage 累加到父 ctx.usage(成本汇总,对齐 hermes)。
    """
    registry: SubagentRegistry | None = parent_ctx.deps.subagent_registry
    child_agent = parent_ctx.deps.subagent_agent
    if child_agent is None:
        raise RuntimeError("未注入子 agent(subagent_agent 为 None)")

    # 父 agent 实际拥有的工具名(用于交集裁剪防越权)
    parent_tool_names = _collect_parent_tool_names(parent_ctx)

    depth = parent_ctx.deps.delegate_depth
    max_spawn = cfg.max_spawn_depth

    semaphore = asyncio.Semaphore(max(1, cfg.max_concurrent_children))

    async def _guarded(spec: SubagentSpec) -> ChildResult:
        # 深度检查
        if depth + 1 > max_spawn and spec.role == "orchestrator":
            return ChildResult(
                subagent_id=f"sa-rejected-{uuid.uuid4().hex[:6]}",
                status="failed",
                summary=f"[委派深度超限(depth={depth+1}, max_spawn_depth={max_spawn}),orchestrator 委派被拒]",
                exit_reason="depth_exceeded",
            )
        if registry is None:
            # 无 registry(异常路径):用一个临时 registry,中断传播只对该次有效
            registry_local = SubagentRegistry()
            async with semaphore:
                return await _run_one_child(spec, child_agent, parent_ctx, registry_local, parent_tool_names, cfg)
        async with semaphore:
            return await _run_one_child(spec, child_agent, parent_ctx, registry, parent_tool_names, cfg)

    tasks = [asyncio.create_task(_guarded(s), name=f"delegate-{i}") for i, s in enumerate(specs)]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=False)
    except asyncio.CancelledError:
        # 父被中断:中断所有子 agent,返回已完成的部分
        if registry is not None:
            await registry.cancel_all()
        # 等待各 task 走完 CancelledError 分支返回 interrupted
        done_results: list[ChildResult] = []
        for t in tasks:
            try:
                r = await t
                done_results.append(r)
            except Exception:
                pass
        results = done_results
        raise
    else:
        # usage 汇总回父
        for r in results:
            if isinstance(r, ChildResult):
                _accumulate_usage(parent_ctx.usage, r.usage)
        return list(results)


def _accumulate_usage(parent_usage: RunUsage, child_usage: RunUsage) -> None:
    """把子 agent 的独立 usage 累加到父(成本汇总,对齐 hermes _child_cost_rollup)。"""
    parent_usage.requests += child_usage.requests
    parent_usage.input_tokens += child_usage.input_tokens
    parent_usage.output_tokens += child_usage.output_tokens


def _collect_parent_tool_names(parent_ctx: RunContext[AgentDeps]) -> set[str]:
    """收集父 agent 当前 run 可用的工具名(用于子 agent 工具集交集裁剪)。

    从 ctx 的 tool_manager / agent 工具集提取。pydantic-ai RunContext 暴露
    ``ctx.tool_manager``(运行时解析的工具集)。降级:若取不到,返回空集表示
    "不做交集限制"(仅靠 DELEGATE_BLOCKED_TOOL_NAMES 兜底)。
    """
    names: set[str] = set()
    tool_manager = getattr(parent_ctx, "tool_manager", None)
    if tool_manager is not None:
        try:
            # pydantic-ai 1.89: tool_manager 在 run 时已解析,有 tools 属性
            tools = getattr(tool_manager, "tools", None)
            if tools:
                for t in tools.values() if isinstance(tools, dict) else tools:
                    name = getattr(t, "name", None) or getattr(getattr(t, "function", None), "__name__", "")
                    if name:
                        names.add(name)
        except Exception:
            logger.debug("failed to collect parent tool names from tool_manager", exc_info=True)
    return names
