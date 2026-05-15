"""Agent 构建逻辑。"""

import logging

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.content.roles import load_role_content
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools import PLANNER_TOOLSET, EXECUTOR_TOOLSET
from openhachimi_agent.agent.intent import TaskFrame


logger = logging.getLogger(__name__)


def _build_base_agent(config: AppConfig, role_name: str, agent_type: str, allowed_tools: set[str] | None = None) -> Agent:
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
    from openhachimi_agent.tools.skills import build_skill_tool
    
    # 动态扫描带 arguments 的技能并注册为宏工具
    dynamic_skill_tools = []
    skills = find_skills(config.skills_dirs)
    for skill in skills:
        if skill.config.arguments and not skill.config.disable_model_invocation:
            dynamic_skill_tools.append(build_skill_tool(skill))
            
    dynamic_toolset = FunctionToolset(tools=dynamic_skill_tools)

    if agent_type == "planner":
        toolsets = [PLANNER_TOOLSET, dynamic_toolset]
        extra_prompt = (
            "\n\n[System Role] 你现在是 **Planner Agent (规划者)**。\n"
            "你的唯一职责是：理解用户目标，然后使用 `create_todos` 制定一个可执行的步骤计划。\n"
            "你自己不要去执行任何调研、搜索或网络请求，那是 Executor 的事。\n\n"
            "Executor 拥有以下工具能力：\n"
            "- 浏览器：browser_navigate（打开URL）、browser_get_state（读取页面）、browser_click、browser_type、browser_scroll、browser_new_tab 等\n"
            "- 网络：web_fetch（HTTP抓取）、web_search（搜索引擎）、discover_web_resources\n"
            "- 文件：read_file、write_file、replace_in_file、list_files、find_files、search_text\n"
            "- 命令行：run_command、send_command_input\n"
            "- Git：git_status、git_diff\n\n"
            "请基于对以上 Executor 工具能力的理解来制定执行计划。\n"
        )
    else:
        filtered_executor_toolset = EXECUTOR_TOOLSET
        if allowed_tools is not None:
            filtered_tools = [t for t in EXECUTOR_TOOLSET.tools if getattr(t, "__name__", "") in allowed_tools or getattr(t, "name", "") in allowed_tools]
            filtered_executor_toolset = FunctionToolset(tools=filtered_tools)
            
        toolsets = [filtered_executor_toolset, dynamic_toolset]
        extra_prompt = "\n\n[System Role] 你现在是 **Executor Agent (执行者)**。你的主要目标是严格按照当前的 TODO 列表，一步步执行具体操作（写代码、运行命令等），并在每一步完成后调用 `update_todo`。不要偏离原定计划！"

    agent = Agent(
        OpenAIChatModel(config.model_name, provider=provider),
        system_prompt=system_prompt + extra_prompt,
        instructions=role_content,
        deps_type=AgentDeps,
        toolsets=toolsets,
        defer_model_check=True,
        retries=3,  # 允许工具调用失败后最多重试 3 次，避免因单次输出格式问题导致整体失败
    )

    @agent.system_prompt
    def _inject_time() -> str:
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return f"[系统环境] 当前真实时间: {current_time}\n"

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
                injected.append(f"<skill name=\"{name}\">\n{skill_map[name].body}\n</skill>")
                
        if injected:
            return "\n\n[System] 以下是基于当前任务意图自动匹配到的专家技能指令，请在执行时严格遵循：\n" + "\n\n".join(injected)
        return ""

    return agent


def build_planner_agent(config: AppConfig, role_name: str) -> Agent:
    """创建专职规划的 Agent。"""
    return _build_base_agent(config, role_name, "planner")


def build_executor_agent(config: AppConfig, role_name: str, allowed_tools: set[str] | None = None) -> Agent:
    """创建专职执行的 Agent（拥有所有权限）。"""
    return _build_base_agent(config, role_name, "executor", allowed_tools=allowed_tools)


def build_router_agent(config: AppConfig) -> Agent:
    """创建用于路由决策的轻量级 Agent。"""
    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )
    
    # 尽可能使用小模型（如果有配置的话，这里为了简单重用同一个配置，但实际中可以用更便宜的模型）
    model = OpenAIChatModel(config.model_name, provider=provider)
    
    skills = find_skills(config.skills_dirs)
    skills_info = "当前可用技能列表（技能名: 描述）：\n"
    if skills:
        for skill in skills:
            skills_info += f"- **{skill.config.name}**: {skill.config.description} (触发时机: {skill.config.when_to_use})\n"
    else:
        skills_info += "无\n"

    system_prompt = (
        "你是一个专业的任务框架分析器。请只做任务理解，不要执行任务。\n"
        "你需要把用户请求整理成 TaskFrame：目标、目标实体、不可变约束、复杂度、风险和是否需要先规划，并挑选可能匹配的技能。\n"
        "- task_kind 可选：qa, code_change, file_ops, shell, browser, research, unknown。\n"
        "- simple：1-2 步即可完成，且低风险。\n"
        "- complex：需要跨文件/多工具/多步骤调研、代码修改、复杂网页操作或系统性分析。\n"
        "- high risk：删除、覆盖、部署、发布、涉及密钥、登录态或不可逆操作。\n"
        "- 如果用户明确给出 URL、文件路径、函数名等目标实体，必须放入 target_entities，并在 invariants 中说明不能替换或扩大目标。\n"
        "- 简单的显式 URL 访问/打开/查看任务应为 browser + simple + requires_plan=false + allowed_autonomy=narrow。\n"
        "- relevant_skills: 如果用户的意图与下方列出的技能匹配，请把匹配的技能名（name）填入该列表。最多选3个。\n"
        "不确定时降低 confidence，并将 requires_plan 设为 true。\n\n"
        f"{skills_info}"
    )
    
    return Agent(
        model,
        system_prompt=system_prompt,
        output_type=TaskFrame,
    )
