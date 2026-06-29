"""主 agent 自主委派子 agent 的工具(pydantic-ai 多 agent 模式① + hermes 委派逻辑)。

单一 ``delegate_task`` 工具:模型调用即触发一个或多个隔离子 agent 跑一轮,返回
结构化汇总文本。核心编排在 :mod:`openhachimi_agent.agent.subagents`(run_delegation)。

隔离哲学(对齐 hermes):子 agent 全新会话、零长期记忆、独立预算、不继承父 history。
父 agent 必须把所需信息(file paths / error / constraints)通过 ``context`` 显式传递。

本模块顶层只导入标准依赖,``run_delegation`` 在函数内延迟导入,打破
subagents ↔ delegation 循环(subagents.py 顶层可安全导入本模块的 DELEGATE_TASK_TOOL)。
"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from openhachimi_agent.agent.execution import with_execution_ledger
from openhachimi_agent.core.deps import AgentDeps


logger = logging.getLogger(__name__)


async def delegate_task(
    ctx: RunContext[AgentDeps],
    goal: str | None = None,
    context: str | None = None,
    toolsets: list[str] | None = None,
    tasks: list[dict] | None = None,
    role: str = "leaf",
    max_iterations: int | None = None,
) -> str:
    """委派一个或多个隔离子 agent 完成指定任务,返回结构化汇总。

    何时用(WHEN TO USE):
    - 推理密集型子任务(调试、代码审查、研究综合)——值得用独立上下文深入推理。
    - 会产生大量中间数据、会污染你(父 agent)主上下文的任务(如遍历很多文件、长搜索结果)。
    - 可并行的独立工作流(同时调研 A 和 B)——传 ``tasks=[...]`` 并发执行。
    - 需要无偏见全新视角的任务——子 agent 是全新会话,不带你的历史包袱。

    何时**不**用(WHEN NOT TO USE,改用别的):
    - 单次工具调用 → 直接调那个工具,不要委派。
    - 机械多步、无需推理 → 自己直接做。
    - 需要用户交互 → 子 agent **不能** clarify_user,它会直接失败返回。
    - 快速文件编辑 → 直接 write_file/replace_in_file。

    关键约束:
    - 子 agent 是**全新会话、零记忆**——它看不到你的对话历史、读不到你的 session_state。
      必须把所需信息(文件路径、错误信息、约束、用户要求的语言/风格)通过 ``context`` 显式传给它。
    - 子 agent 返回的是 **self-report(自述)**,不是已验证事实。对有外部副作用的操作,
      要求子 agent 返回可验证的句柄(文件路径/命令输出),你(父 agent)自己再验证一遍。
    - leaf 子 agent(默认)不能再委派、不能 clarify、不能写长期记忆、不能改你的 TODO 计划。

    参数:
    - ``goal``:单个任务的目标描述(与 ``tasks`` 二选一)。
    - ``context``:传给子 agent 的上下文(文件路径/错误/约束等)——**子 agent 零记忆,必传**。
    - ``toolsets``:子 agent 可用的工具组名,如 ``["file","web"]``;默认只读 ``["read","web","git","skills"]``。
    - ``tasks``:批量并发任务,每个是 ``{"goal":..., "context":..., "toolsets":[...]}``;并发数受配置限制。
    - ``role``:``"leaf"``(默认,不可再委派)或 ``"orchestrator"``(可再委派孙子,受 max_spawn_depth 限制)。
    - ``max_iterations``:单个子 agent 工具调用轮次上限;默认用配置值。

    返回:各子 agent 的 summary 汇总文本(completed/timeout/interrupted/failed 状态标注)。
    """
    # 延迟导入打破循环
    from openhachimi_agent.agent.subagents import SubagentSpec, run_delegation

    child_agent = ctx.deps.subagent_agent
    if child_agent is None:
        logger.warning("delegate_task called but no subagent_agent injected (run_mode=%s)", ctx.deps.run_mode)
        return "[delegate_task 不可用:未注入子 agent。请直接用 read_file/search_text/web_search 等自有工具完成。]"

    # 组装 specs:tasks 优先,否则用 goal
    if tasks:
        specs = [
            SubagentSpec(
                goal=str(t.get("goal") or ""),
                context=str(t.get("context") or ""),
                toolsets=t.get("toolsets"),
                role=str(t.get("role") or role),
                max_iterations=t.get("max_iterations"),
            )
            for t in tasks
        ]
    elif goal:
        specs = [SubagentSpec(goal=goal, context=context or "", toolsets=toolsets, role=role, max_iterations=max_iterations)]
    else:
        return "[delegate_task 参数错误:必须提供 goal 或 tasks。]"

    cfg = ctx.deps.config.delegation

    try:
        results = await run_delegation(specs, ctx, cfg)
    except Exception as exc:
        logger.warning("run_delegation failed: %s", exc, exc_info=True)
        return f"[delegate_task 执行失败: {exc}]"

    # 组装结构化汇总文本返回父 agent
    lines = [f"## 委派结果({len(results)} 个子 agent)"]
    for i, r in enumerate(results, 1):
        status_tag = {"completed": "✅", "timeout": "⏱", "interrupted": "🛑", "failed": "❌"}.get(r.status, "?")
        lines.append(f"\n### 子 agent {i} [{status_tag} {r.status}] ({r.subagent_id})")
        if r.exit_reason and r.exit_reason != r.status:
            lines.append(f"exit: {r.exit_reason}")
        lines.append(r.summary or "(无输出)")
    return "\n".join(lines)


# 包 ledger 后导出,与 tools/registry.py 里其它工具的包装风格一致。
# 失败/超时/中断都会被 ledger 记录,让 _replan_after_execution_signal 能感知委派成败。
DELEGATE_TASK_TOOL = with_execution_ledger(delegate_task)
