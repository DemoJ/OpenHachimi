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
from openhachimi_agent.memory.recall import build_memory_context_text
from openhachimi_agent.tools import PLANNER_TOOLSET, EXECUTOR_TOOLSET, SCHEDULED_EXECUTOR_TOOLSET
from openhachimi_agent.agent.intent import PlanContinuationDecision, TaskFrame


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
    import datetime

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
        retries=3,  # 允许工具调用失败后最多重试 3 次，避免因单次输出格式问题导致整体失败
    )

    if agent_type in {"executor", "scheduled_executor"}:
        @agent.output_validator
        def _validate_execution_result(ctx: RunContext[AgentDeps], result: str) -> str:
            from openhachimi_agent.agent.execution import get_final_verification_signal
            import json
            from pydantic_ai.exceptions import ModelRetry
            
            signal = get_final_verification_signal(ctx.deps.session_state)
            if signal:
                raise ModelRetry(
                    f"[系统拦截] 你不能现在就结束任务并回复最终结果！当前 TODO 列表中仍有未完成的任务，或最后一次执行失败。\n"
                    f"验证详情：{json.dumps(signal, ensure_ascii=False)}\n"
                    f"请务必先调用 `update_todo` 工具将所有完成的任务状态更新为 done，或者继续调用工具执行未完成的步骤。"
                )
            return result

    @agent.system_prompt
    def _time_prompt(ctx: RunContext[AgentDeps]) -> str:
        # 使用 isoformat() 保证时区信息明确，并且对模型最友好
        current_time = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
        return render_system_prompt("runtime/time", {"current_time": current_time}) + "\n"

    @agent.system_prompt
    def _config_prompt(ctx: RunContext[AgentDeps]) -> str:
        return render_system_prompt("runtime/config", {"user_dir": str(config.user_dir).replace("\\", "/")}) + "\n"

    @agent.system_prompt
    def _inject_memory_context(ctx: RunContext[AgentDeps]) -> str:
        if not ctx.deps.config.memory.enabled:
            return ""
        return build_memory_context_text(ctx.deps.config, ctx.deps.memory_context)

    @agent.system_prompt
    def _inject_matched_skills(ctx: RunContext[AgentDeps]) -> str:
        task_frame_dict = ctx.deps.session_state.get("task_frame")
        if not task_frame_dict:
            return ""
        
        relevant_skills = task_frame_dict.get("relevant_skills", [])
        if not relevant_skills:
            return ""

        skills = find_skills(ctx.deps.skills_dirs)
        skill_map = {s.config.name: s for s in skills}
        
        injected = []
        for name in relevant_skills:
            if name in skill_map:
                injected.append(format_skill_prompt(skill_map[name]))

        if injected:
            return render_system_prompt("runtime/matched_skills", {"skills": "\n\n".join(injected)})
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
