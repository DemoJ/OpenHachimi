"""Agent 构建逻辑。"""

import logging

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.content.prompts import load_system_prompt, render_system_prompt
from openhachimi_agent.content.roles import load_role_content
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools import PLANNER_TOOLSET, EXECUTOR_TOOLSET, SCHEDULED_EXECUTOR_TOOLSET
from openhachimi_agent.agent.intent import PlanContinuationDecision, SelfCritiqueDecision, TaskFrame


logger = logging.getLogger(__name__)


def _build_base_agent(config: AppConfig, role_name: str, agent_type: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None) -> Agent:
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

    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )
    
    from pydantic_ai import FunctionToolset
    from openhachimi_agent.tools.skills import build_skill_tool, format_skill_prompt
    
    # 动态扫描带 arguments 的技能并注册为宏工具
    dynamic_skill_tools = []
    skills = find_skills(config.skills_dirs)
    for skill in skills:
        if skill.config.arguments and not skill.config.disable_model_invocation:
            dynamic_skill_tools.append(build_skill_tool(skill))
            
    dynamic_toolset = FunctionToolset(tools=dynamic_skill_tools)

    mcp_toolsets = mcp_toolsets or []

    if agent_type == "planner":
        toolsets = [PLANNER_TOOLSET, dynamic_toolset] + mcp_toolsets
        extra_prompt = load_system_prompt("agents/planner")
    else:
        base_executor_toolset = SCHEDULED_EXECUTOR_TOOLSET if agent_type == "scheduled_executor" else EXECUTOR_TOOLSET
        filtered_executor_toolset = base_executor_toolset
        if allowed_tools is not None:
            filtered_tools = [
                t for t in base_executor_toolset.tools
                if getattr(t, "__name__", "") in allowed_tools or getattr(t, "name", "") in allowed_tools
            ]
            filtered_executor_toolset = FunctionToolset(tools=filtered_tools)

        toolsets = [filtered_executor_toolset, dynamic_toolset] + mcp_toolsets
        if agent_type == "scheduled_executor":
            extra_prompt = load_system_prompt("agents/scheduled_executor")
        else:
            extra_prompt = load_system_prompt("agents/executor")

    agent = Agent(
        OpenAIChatModel(config.model_name, provider=provider),
        system_prompt=system_prompt + "\n\n" + extra_prompt,
        instructions=role_content,
        deps_type=AgentDeps,
        toolsets=toolsets,
        defer_model_check=True,
        # retries 既覆盖工具调用错误,也覆盖 output_validator 抛出的 ModelRetry。
        # 一次工具偶发失败(1) + 一次 final-answer validator 打回(1) + 一次 schema
        # 修正(1) 已耗光 retries=3 → UnexpectedModelBehavior。这里给到 5 是为
        # validator 重试链留出预算,真正的死循环熔断由 validator 内部计数器和
        # executor.py 的 replan 兜底负责,不能只靠抬上限。
        retries=5,
    )

    if agent_type in {"executor", "scheduled_executor"}:
        # 同一轮内 validator 连续打回的次数,用于硬熔断。在 execute_task 入口处
        # 会清零(见 service.agent_runtime.executor.execute_task)。
        VALIDATOR_RETRY_KEY = "_final_validator_retries"
        VALIDATOR_HARD_LIMIT = 2  # 第 0、1 次打回,第 2 次强制放行避免 UnexpectedModelBehavior

        @agent.output_validator
        def _validate_execution_result(ctx: RunContext[AgentDeps], result: str) -> str:
            from openhachimi_agent.agent.execution import (
                _append_ledger_event,
                get_final_verification_signal,
            )
            import json
            from pydantic_ai.exceptions import ModelRetry

            signal = get_final_verification_signal(ctx.deps.session_state)
            if not signal:
                return result

            session_state = ctx.deps.session_state
            counter = int(session_state.get(VALIDATOR_RETRY_KEY, 0) or 0)

            # 同时把"validator 打回"记入 execution_ledger,让上层 executor.py 的
            # _replan_after_execution_signal 能感知到这条卡死的链条,在合适时机
            # 触发 replan(get_replan_signal 会看到连续 blocked 事件)。
            try:
                _append_ledger_event(
                    session_state,
                    tool_name="<final_answer_validator>",
                    status="blocked",
                    args={"attempt": counter + 1},
                    result=signal,
                    violation=(
                        json.dumps(signal, ensure_ascii=False)[:500]
                        if signal
                        else ""
                    ),
                )
            except Exception:
                logger.debug("failed to append validator event to ledger", exc_info=True)

            # 硬熔断:已经被打回 >=VALIDATOR_HARD_LIMIT 次,放行,把模型的话给用户。
            # 兜底"无解任务"场景(工具/权限缺失、用户输入不完整、模型坚持自己已完成等),
            # 避免 UnexpectedModelBehavior 把对话整轮报废。模型本来想说的话会和
            # 系统追加的"[System] 任务未完成"提示一起返回,由 executor.py 决定追加方式。
            if counter >= VALIDATOR_HARD_LIMIT:
                logger.warning(
                    "final answer validator yielding after %d retries to avoid loop "
                    "(session=%s); raw signal=%s",
                    counter,
                    ctx.deps.session_id,
                    json.dumps(signal, ensure_ascii=False)[:200],
                )
                session_state["_final_validator_yielded"] = True
                session_state["_final_validator_last_signal"] = signal
                return result

            session_state[VALIDATOR_RETRY_KEY] = counter + 1

            # 把未完成的 TODO 详情直接列在 ModelRetry 里。原版只说"请调用 update_todo
            # 将所有完成的任务状态更新为 done"——但任务从未真正完成,模型按指示
            # 标 done 等同于撒谎;不按又陷入死循环。新版给出具体可操作的下一步,
            # 让模型从"该不该标 done"转向"先 get_todos 看清状态、再 in-progress、
            # 再调真实工具",打破认知误区。
            issues = signal.get("issues", []) if isinstance(signal, dict) else []
            unfinished_lines: list[str] = []
            latest_failure_lines: list[str] = []
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                if issue.get("type") == "unfinished_todos":
                    for item in issue.get("items", [])[:10]:
                        if isinstance(item, dict):
                            unfinished_lines.append(
                                f"  - [{item.get('status', '?')}] #{item.get('id', '?')} {item.get('description', '')}"
                            )
                elif issue.get("type") == "latest_execution_not_successful":
                    latest_failure_lines.append(
                        f"  - 上一次 `{issue.get('tool_name', '?')}` 以 `{issue.get('status', '?')}` 结束："
                        f"{(issue.get('detail') or '')[:300]}"
                    )

            parts = [
                f"[系统拦截] 你不能现在就给用户最终回复——当前轮的任务尚未真正完成（第 {counter + 1} 次拦截）。",
            ]
            if unfinished_lines:
                parts.append("未完成的 TODO：\n" + "\n".join(unfinished_lines))
            if latest_failure_lines:
                parts.append("最近一次工具调用没有成功：\n" + "\n".join(latest_failure_lines))
            parts.append(
                "请按以下顺序操作，**不要给用户回复任何文字**：\n"
                "1. 调用 `get_todos` 查看完整列表；\n"
                "2. 挑出第一个 status=pending 且依赖已 done 的任务，"
                "用 `update_todo(id, \"in-progress\")` 标记；\n"
                "3. 调用相应执行工具（write_file/run_command/web_fetch/research_sources 等）真正完成它；\n"
                "4. 完成后 `update_todo(id, \"done\", notes=...)`，再继续下一项；\n"
                "5. 如果某项确实**无法**继续（外部条件缺失、需要用户决策），"
                "用 `update_todo(id, \"blocked\", notes=\"原因\")` 明确标记，"
                "然后在最终回复里清楚告知用户：已完成什么 / 卡在哪 / 需要什么——"
                "不要装作完成。"
            )
            parts.append(
                "禁止在文字里模仿工具返回格式（如 \"✅ 任务 X → done\"）；"
                "系统能区分真假，假陈述会再次被打回。"
            )

            raise ModelRetry("\n\n".join(parts))

    @agent.system_prompt
    def _config_prompt(ctx: RunContext[AgentDeps]) -> str:
        return render_system_prompt("runtime/config", {"user_dir": str(config.user_dir).replace("\\", "/")}) + "\n"

    # 每轮易变运行时上下文（当前时间 / TaskFrame 摘要 / 长期记忆召回 / 命中技能定义）
    # 通过 pydantic-ai 的 @agent.system_prompt 动态钩子注入 system prompt 末尾。
    #
    # 之所以放 system prompt 而不是 user-prompt：
    # - 这些内容在语义上属于"系统给模型的上下文"，不是用户说的话。如果塞到
    #   user-prompt 前缀，capture_turn_memories 会把"<memory-context>...<SKILL>..."
    #   也作为"用户输入"抽进长期记忆，雪球越滚越大。
    # - system prompt 是按 token 前缀渐进式命中 KV cache 的，前面稳定主体仍能
    #   完整命中；只损失末尾几十~几百 token，远低于把万字 SKILL 塞进 user-prompt
    #   的代价。
    # - 动态钩子每次 run 都重新计算，跨天/跨长会话仍能拿到当下时间和当下记忆，
    #   不会出现"几天后模型还以为是几天前"的问题。
    @agent.system_prompt
    def _runtime_dynamic_block(ctx: RunContext[AgentDeps]) -> str:
        try:
            from openhachimi_agent.content.runtime_context import build_system_dynamic_block

            return build_system_dynamic_block(ctx.deps)
        except Exception:  # noqa: BLE001  动态注入失败不应阻断 agent run
            logger.exception("runtime dynamic block failed")
            return ""

    return agent


def build_planner_agent(config: AppConfig, role_name: str, mcp_toolsets: list | None = None) -> Agent:
    """创建专职规划的 Agent。"""
    return _build_base_agent(config, role_name, "planner", mcp_toolsets=mcp_toolsets)


def build_executor_agent(config: AppConfig, role_name: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None) -> Agent:
    """创建专职执行的 Agent（拥有所有权限）。"""
    return _build_base_agent(config, role_name, "executor", allowed_tools=allowed_tools, mcp_toolsets=mcp_toolsets)


def build_scheduled_executor_agent(config: AppConfig, role_name: str, allowed_tools: set[str] | None = None, mcp_toolsets: list | None = None) -> Agent:
    """创建定时任务执行 Agent（不暴露调度写入工具）。"""
    return _build_base_agent(config, role_name, "scheduled_executor", allowed_tools=allowed_tools, mcp_toolsets=mcp_toolsets)


def _build_router_model(config: AppConfig) -> OpenAIChatModel:
    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )
    return OpenAIChatModel(config.model_name, provider=provider)


def build_router_agent(config: AppConfig) -> Agent:
    """创建用于路由决策的轻量级 Agent。"""
    model = _build_router_model(config)

    skills = find_skills(config.skills_dirs)
    skills_info = "当前可用技能列表（技能名: 描述）：\n"
    if skills:
        for skill in skills:
            skills_info += f"- **{skill.config.name}**: {skill.config.description} (触发时机: {skill.config.when_to_use})\n"
    else:
        skills_info += "无\n"

    system_prompt = render_system_prompt("agents/router", {"skills_info": skills_info})
    
    return Agent(
        model,
        system_prompt=system_prompt,
        output_type=TaskFrame,
    )


def build_continuation_agent(config: AppConfig) -> Agent:
    """创建用于判断用户是否要继续旧计划的轻量级 Agent。"""
    system_prompt = load_system_prompt("agents/continuation")
    return Agent(
        _build_router_model(config),
        system_prompt=system_prompt,
        output_type=PlanContinuationDecision,
    )


def build_self_critique_agent(config: AppConfig) -> Agent:
    """创建用于最终答案自检的轻量级 Agent。"""
    system_prompt = load_system_prompt("agents/self_critique")
    return Agent(
        _build_router_model(config),
        system_prompt=system_prompt,
        output_type=SelfCritiqueDecision,
    )
