"""Agent 构建逻辑。"""

import logging

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import DeferredToolRequests

from openhachimi_agent.content.prompts import load_system_prompt, render_system_prompt
from openhachimi_agent.content.role_filters import filter_mcp_toolsets_for_role, filter_skills_for_role, get_role_filters
from openhachimi_agent.content.roles import load_role_content
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools import MAIN_TOOLSET


logger = logging.getLogger(__name__)


def _latest_failure_issues(signal: dict | None) -> list[dict]:
    """取出 final verification signal 里"本轮最近一次工具调用失败"的 issue。"""
    if not isinstance(signal, dict):
        return []
    return [
        issue for issue in signal.get("issues", [])
        if isinstance(issue, dict) and issue.get("type") == "latest_execution_not_successful"
    ]


def _build_latest_failure_retry_message(issues: list[dict]) -> str:
    """最近一次工具调用失败时打回给模型的提示。只打回一次,允许模型如实汇报失败。"""
    detail_lines = [
        f"  - `{issue.get('tool_name', '?')}` 以 `{issue.get('status', '?')}` 结束："
        f"{(issue.get('detail') or '')[:300]}"
        for issue in issues
    ]
    parts = ["[系统提醒] 本轮最近一次工具调用没有成功。"]
    if detail_lines:
        parts.append("失败详情：\n" + "\n".join(detail_lines))
    parts.append(
        "请先判断原因：\n"
        "- 能修正（参数错、路径不对、可以换工具或换数据源）就修正后重试；\n"
        "- 确实无法恢复（外部条件缺失、需要用户决策）就直接回复用户，如实说明失败原因、"
        "已完成什么、还需要什么。有对应的 TODO 项时，先用 "
        "`update_todo(id, \"blocked\", notes=\"原因\")` 标记。\n"
        "不要在文字里模仿工具返回格式（如 \"✅ 任务 X → done\"）。"
    )
    return "\n\n".join(parts)


def _record_validator_retry(session_state: dict, signal: dict, attempt: int) -> None:
    """把一次 validator 打回记进 execution ledger,便于事后排查。失败不影响主流程。"""
    import json

    from openhachimi_agent.agent.execution import _append_ledger_event

    try:
        _append_ledger_event(
            session_state,
            tool_name="<final_answer_validator>",
            status="blocked",
            args={"attempt": attempt},
            result=signal,
            violation=json.dumps(signal, ensure_ascii=False)[:500],
        )
    except Exception:
        logger.debug("failed to append validator event to ledger", exc_info=True)


def _build_reasoning_model_settings(config: AppConfig) -> dict:
    """把 llm_reasoning_effort 配置翻译成 pydantic-ai 的 model_settings。

    值即 openai SDK 官方 reasoning_effort 枚举,并额外支持 max(none/minimal/low/medium/high/xhigh/max),
    原样透传给 OpenAIChatModel。none 也显式发送,行为与官方参数一致;对不支持
    reasoning 的模型由服务端自行处理。
    """
    return {"openai_reasoning_effort": config.llm_reasoning_effort}


def _build_model_profile(config: AppConfig) -> dict | None:
    """按配置构建 OpenAIChatModel profile,用于 thinking 字段回传兼容。

    留空(默认)返回 None:pydantic-ai 走 auto 模式,自动按响应里的字段名回传,
    对 OpenAI 官方模型等零影响;仅在目标网关要求"thinking mode 必须回传
    reasoning_content"且多轮工具调用会被 400 拒掉时,配置
    ``llm.thinking_field=reasoning_content`` 启用 field 模式 —— 除按该字段回传
    思考内容外,pydantic-ai 还会给纯工具调用轮次的 assistant 消息补空字段,
    满足网关校验。
    """
    if not config.llm_thinking_field:
        return None
    return {
        "openai_chat_thinking_field": config.llm_thinking_field,
        "openai_chat_send_back_thinking_parts": "field",
    }


def _build_base_agent(config: AppConfig, role_name: str, agent_type: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None, run_mode: str = "interactive") -> Agent:
    if not config.openai_api_key:
        raise ValueError("未配置 llm.api_key，请先在 user/config.yaml 中填写 API Key。")

    logger.info(
        "building %s agent role=%s model=%s base_url_configured=%s",
        agent_type,
        role_name,
        config.model_name,
        bool(config.openai_base_url),
    )

    system_prompt = load_system_prompt()
    role_content = load_role_content(config.roles_dir, role_name)
    # 角色级 skills/MCP 绑定:未配置的角色 get_role_filters 返回全 all(= 历史全局行为)。
    role_filters = get_role_filters(config, role_name)

    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )

    from pydantic_ai import FunctionToolset
    from openhachimi_agent.tools.skills import build_skill_tool, format_skill_prompt

    # 动态扫描带 arguments 的技能并注册为宏工具;先按角色绑定过滤可见 skill 集。
    # (与 runtime_context._skills_index_block 的过滤一致,两处共用 role_filters 逻辑。)
    dynamic_skill_tools = []
    skills = filter_skills_for_role(role_filters, find_skills(config.skills_dirs))
    for skill in skills:
        if skill.config.arguments and not skill.config.disable_model_invocation:
            dynamic_skill_tools.append(build_skill_tool(skill))

    dynamic_toolset = FunctionToolset(tools=dynamic_skill_tools)

    # mcp_toolsets 由 service 传入为带名列表 [(server_name, ts), ...];
    # 按角色绑定过滤后只取实例加入 toolsets。未选中的 server 仍保持连接,
    # 只是不对该角色暴露。
    mcp_toolsets = mcp_toolsets or []
    filtered_mcp = filter_mcp_toolsets_for_role(role_filters, mcp_toolsets)
    mcp_instances = [ts for _name, ts in filtered_mcp]

    # ── 主 agent / subagent 两分支 ──────────────────────────────────────
    # Hermes 式重构后只有两类 agent:
    # - main:单一主 agent,持全套工具(含 create_todos/clarify_user/delegate_task),
    #   output_type=[str, DeferredToolRequests](兼容 clarify_user 的 CallDeferred)。
    #   todo 是普通工具,主 agent 自主决定要不要建计划;停止时由 output_validator
    #   闸门(verification_stop + final_verification_signal)校验完整性。
    # - subagent:delegate_task 委派的子 agent,全新会话零记忆,str 输出,
    #   主工具集运行时由 resolve_child_toolsets 裁剪注入,构建时只挂骨架。
    #
    # scheduled 模式不再单独建 agent 实例:走 main agent,注入 scheduled_executor
    # prompt(见下方 main 分支);scheduler 写工具由 scheduler 工具自身的
    # ``ensure_scheduler_mutation_allowed`` 在 run_mode=scheduled 时拦截。
    if agent_type == "main":
        output_type: object = [str, DeferredToolRequests]
    else:  # subagent
        output_type = str

    if agent_type == "subagent":
        # 通用子 agent(对齐 hermes delegate_task 委派的 child)。
        # 构建时只挂骨架工具集(dynamic_toolset 角色技能 + mcp),主工具集(read/file/web/
        # browser/terminal 等)不在此固定——运行时由 delegate_task 经 resolve_child_toolsets
        # 按 toolsets 参数裁剪后,通过 child.run(..., toolsets=[resolved]) 注入。
        # 隔离哲学:子 agent 全新会话、零记忆、独立预算(见 agent/subagents.py)。
        # 不注册 output_validator(subagent 走 str fail-fast,不像主 agent 那样校验 TODO)。
        toolsets = [dynamic_toolset] + mcp_instances
        extra_prompt = load_system_prompt("agents/subagent")
    else:  # main
        base_main_toolset = MAIN_TOOLSET
        if allowed_tools is not None:
            filtered_tools = [
                t for t in base_main_toolset.tools
                if getattr(t, "__name__", "") in allowed_tools or getattr(t, "name", "") in allowed_tools
            ]
            base_main_toolset = FunctionToolset(tools=filtered_tools)

        if run_mode == "scheduled" and not getattr(
            getattr(config.scheduler, "security", None), "allow_dangerous_tools_in_scheduled_runs", False
        ):
            # 定时任务无人值守执行:默认剔除高危工具(命令执行/文件写入删除/技能安装)。
            # 无人值守场景没有用户确认兜底,任务提示词可能被外部消息注入,
            # 只保留只读、检索与报告产出类工具;确有需要可经配置放开。
            restricted = {
                "run_command", "send_command_input",
                "write_file", "replace_in_file", "delete_path", "install_skill",
            }
            trimmed_tools = [
                t for t in base_main_toolset.tools
                if getattr(t, "__name__", "") not in restricted and getattr(t, "name", "") not in restricted
            ]
            base_main_toolset = FunctionToolset(tools=trimmed_tools)

        toolsets = [base_main_toolset, dynamic_toolset] + mcp_instances
        if run_mode == "scheduled":
            # 定时任务无人值守:复用主 agent + 主 prompt,额外注入 scheduled_executor
            # 提示词(明确不暴露调度写入、不主动 clarify_user)。
            extra_prompt = load_system_prompt("agents/main_agent") + "\n\n" + load_system_prompt("agents/scheduled_executor")
        else:
            extra_prompt = load_system_prompt("agents/main_agent")

    # 最终系统指令顺序（从上到下,按 KV cache 前缀命中率优化）:
    #   1. base.md               — 系统人格、核心原则、安全边界
    #   2. role_content          — 角色设定（用户级 user/roles/<name>.md）
    #   3. extra_prompt          — main_agent.md / subagent.md 操作规范
    #   4. @agent.instructions   — config 块 + executor 专用按需块(半稳定)
    #   5. @agent.instructions   — 记忆召回(随记忆写入变化)
    #   6. @agent.instructions   — 当前时间(每轮必变)
    # 越靠前越稳定、越靠后越易变:前面几乎不变的主体可完整命中前缀缓存,
    # 每轮失效的只有末尾几十~几百 token。
    #
    # 用 instructions 而不是 system_prompt:pydantic-ai 只在 message_history 为空时生成
    # system_prompt,之后它作为第一条消息留在历史里原样复用——记忆召回、当前时间、
    # TODO 接力这些"每轮动态"块实际上从第二轮起就冻结在第一轮的内容。instructions
    # 每次请求都按当前状态重新计算,也不会从历史里读回旧内容。旧会话历史里残留的
    # system prompt 由 main_agent.strip_frozen_system_prompt 在运行前清掉。
    model = OpenAIChatModel(
        config.model_name,
        provider=provider,
        profile=_build_model_profile(config),
    )
    if config.context.enabled:
        from openhachimi_agent.context.request_budget import ContextBudgetModel

        model = ContextBudgetModel(
            model, context_length=config.context.context_length,
            hard_ceiling_percent=config.context.hard_ceiling_percent,
        )
    agent = Agent(
        model,
        instructions=system_prompt + "\n\n" + role_content + "\n\n" + extra_prompt,
        deps_type=AgentDeps,
        toolsets=toolsets,
        output_type=output_type,
        defer_model_check=True,
        # retries 既覆盖工具调用错误,也覆盖 output_validator 抛出的 ModelRetry。
        # 一次工具偶发失败(1) + 一次 final-answer validator 打回(1) + 一次 schema
        # 修正(1) 已耗光 retries=3 → UnexpectedModelBehavior。这里给到 5 是为
        # validator 重试链留出预算,真正的死循环熔断由 validator 内部计数器负责,
        # 不能只靠抬上限。
        retries=5,
        model_settings=_build_reasoning_model_settings(config),
    )

    if agent_type == "main":
        agent.output_validator(_validate_execution_result)

    @agent.instructions
    def _config_prompt(ctx: RunContext[AgentDeps]) -> str:
        return render_system_prompt("runtime/config", {"user_dir": str(config.user_dir).replace("\\", "/")}) + "\n"

    # 注册顺序即系统指令拼装顺序:executor 专用块必须先于 _runtime_dynamic_block ——
    # 技能索引/TODO 接力随会话状态变化,但比记忆/时间稳定;按"越易变越靠后"排列才能
    # 最大化 KV cache 前缀命中(见本函数开头的顺序注释)。subagent 不注册 executor 专用块。
    if agent_type == "main":
        agent.instructions(_executor_extra_block)
    # 必须是最后一个注册的指令钩子:记忆块垫底、时间块收尾,每轮必变的时间位于绝对末尾。
    agent.instructions(_runtime_dynamic_block)
    return agent


# 同一轮内 validator 连续打回的次数,用于熔断。run_main_agent / resume_main_agent 入口处清零。
VALIDATOR_RETRY_KEY = "_final_validator_retries"
# 最近一次工具失败时只打回 1 次:第二次模型多半是在如实汇报失败,再拦只会浪费调用。
VALIDATOR_HARD_LIMIT = 1


def _verification_gap_nudge(ctx: RunContext[AgentDeps]) -> str | None:
    """闸门 1:改了代码却没验证就想结束时返回提醒文案(内部有次数上限),否则 None。

    照搬 Hermes verification_stop;透传 session_store/session_id 让提醒能引用"上次验证"证据。
    """
    from openhachimi_agent.agent.execution import _append_ledger_event
    from openhachimi_agent.agent.verification_stop import build_verify_on_stop_nudge

    session_state = ctx.deps.session_state
    nudge = build_verify_on_stop_nudge(
        session_state,
        session_store=getattr(ctx.deps, "session_store", None),
        session_id=ctx.deps.session_id,
    )
    if nudge:
        try:
            _append_ledger_event(
                session_state,
                tool_name="<verification_stop>",
                status="blocked",
                args={"reason": "edited_without_fresh_evidence"},
                result=nudge,
                violation=nudge[:500],
            )
        except Exception:
            logger.debug("failed to append verification_stop event to ledger", exc_info=True)
    return nudge


def _validate_execution_result(ctx: RunContext[AgentDeps], result: str) -> str:
    """主 agent 的收尾校验(output_validator)。

    闸门 1:改了代码没验证 → 打回提醒验证。
    闸门 2:final verification signal 里两类 issue ——
      - unfinished_todos:计划仍有 pending/in-progress 项。不打回,也不往回复里拼提醒:回复
        发出后 turn_stream 会统一向用户列出未完成项。拼进回复会显示两遍并污染历史。
      - latest_execution_not_successful:本轮最近一次工具调用失败。打回一次,让模型修正重试
        或如实汇报;第二次放行。
    """
    import json

    from pydantic_ai.exceptions import ModelRetry

    from openhachimi_agent.agent.execution import get_final_verification_signal

    from openhachimi_agent.agent.answer_attempts import mark_latest_answer

    messages = getattr(ctx, "messages", ())
    verify_nudge = _verification_gap_nudge(ctx)
    if verify_nudge:
        mark_latest_answer(messages, "rejected")
        raise ModelRetry(verify_nudge)

    session_state = ctx.deps.session_state
    signal = get_final_verification_signal(session_state)
    latest_failure_issues = _latest_failure_issues(signal)
    if not latest_failure_issues:
        mark_latest_answer(messages, "committed")
        return result

    counter = int(session_state.get(VALIDATOR_RETRY_KEY, 0) or 0)
    _record_validator_retry(session_state, signal, counter + 1)
    if counter >= VALIDATOR_HARD_LIMIT:
        logger.warning(
            "final answer validator yielding after %d retries to avoid loop (session=%s); raw signal=%s",
            counter,
            ctx.deps.session_id,
            json.dumps(signal, ensure_ascii=False)[:200],
        )
        session_state["_final_validator_yielded"] = True
        session_state["_final_validator_last_signal"] = signal
        mark_latest_answer(messages, "committed")
        return result
    session_state[VALIDATOR_RETRY_KEY] = counter + 1
    mark_latest_answer(messages, "rejected")
    raise ModelRetry(_build_latest_failure_retry_message(latest_failure_issues))


def _executor_extra_block(ctx: RunContext[AgentDeps]) -> str:
    """主 agent 专用的按需指令块:规划引导或 TODO 接力、产物落点、技能索引、计划进度快照。"""
    try:
        from openhachimi_agent.content.runtime_context import build_executor_extra_dynamic_block

        return build_executor_extra_dynamic_block(ctx.deps)
    except Exception:  # noqa: BLE001
        logger.exception("executor extra dynamic block failed")
        return ""


def _runtime_dynamic_block(ctx: RunContext[AgentDeps]) -> str:
    """每轮易变的运行时上下文(长期记忆召回 / 当前时间),放在系统指令最末尾。

    放系统指令而不是 user-prompt:这些是"系统给模型的上下文",塞进 user-prompt 会被
    capture_turn_memories 当成用户输入抽进长期记忆,越滚越大。
    """
    try:
        from openhachimi_agent.content.runtime_context import build_system_dynamic_block

        return build_system_dynamic_block(ctx.deps)
    except Exception:  # noqa: BLE001  动态注入失败不应阻断 agent run
        logger.exception("runtime dynamic block failed")
        return ""


def build_main_agent(config: AppConfig, role_name: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None, run_mode: str = "interactive") -> Agent:
    """创建单一主 Agent(Hermes 式:持全套工具 + todo 普通工具 + verification 闸门)。

    ``run_mode="scheduled"`` 时复用主 agent,但额外注入 scheduled_executor 提示词,
    scheduler 写工具由 scheduler 工具自身的 ``ensure_scheduler_mutation_allowed`` 拦截。
    """
    return _build_base_agent(config, role_name, "main", allowed_tools=allowed_tools, mcp_toolsets=mcp_toolsets, run_mode=run_mode)


def build_subagent_agent(config: AppConfig, role_name: str, mcp_toolsets: list | None = None) -> Agent:
    """创建通用子 Agent(对齐 hermes delegate_task 委派的 child)。

    供主 agent 通过 ``delegate_task`` 工具委派(pydantic-ai 多 agent 模式 +
    hermes 隔离哲学)。子 agent 全新会话、零长期记忆、独立预算;主工具集运行时
    由 :func:`openhachimi_agent.agent.subagents.resolve_child_toolsets` 裁剪注入,
    构建时不固定(只挂 dynamic_toolset + mcp 骨架)。
    """
    return _build_base_agent(config, role_name, "subagent", mcp_toolsets=mcp_toolsets)
