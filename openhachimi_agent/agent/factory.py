"""Agent 构建逻辑。"""

import logging

from pydantic_ai import Agent
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


def _build_base_agent(config: AppConfig, role_name: str, agent_type: str) -> Agent:
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

    # 动态扫描可用技能，将轻量级菜单注入到系统提示词中
    skills = find_skills(config.skills_dirs)
    if skills:
        skills_menu = ["\n\n## 🛠️ 当前工作区可用技能 (Skills) 菜单", "以下是当前项目已经安装的可用技能，当用户请求符合以下场景时，请主动调用 `get_skill_instructions` 查阅并执行它们："]
        for skill in skills:
            entry = f"- **{skill.config.name}**: {skill.config.description}"
            if skill.config.when_to_use:
                entry += f" (触发时机: {skill.config.when_to_use})"
            skills_menu.append(entry)
        system_prompt += "\n".join(skills_menu)

    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )

    if agent_type == "planner":
        toolsets = [PLANNER_TOOLSET]
        extra_prompt = "\n\n[System Role] 你现在是 **Planner Agent (规划者)**。你**没有**执行破坏性操作（如写文件、命令行、浏览器深度交互）的权限。你的唯一任务是通过阅读文件和搜索网络，充分了解任务背景，然后使用 `create_todos` 制定详尽的执行计划。"
    else:
        toolsets = [EXECUTOR_TOOLSET]
        extra_prompt = "\n\n[System Role] 你现在是 **Executor Agent (执行者)**。你的主要目标是严格按照当前的 TODO 列表，一步步执行具体操作（写代码、运行命令等），并在每一步完成后调用 `update_todo`。不要偏离原定计划！"

    agent = Agent(
        OpenAIChatModel(config.model_name, provider=provider),
        system_prompt=system_prompt + extra_prompt,
        instructions=role_content,
        deps_type=AgentDeps,
        toolsets=toolsets,
        defer_model_check=True,
    )

    @agent.system_prompt
    def _inject_time() -> str:
        current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return f"[系统环境] 当前真实时间: {current_time}\n"

    return agent


def build_planner_agent(config: AppConfig, role_name: str) -> Agent:
    """创建专职规划的 Agent。"""
    return _build_base_agent(config, role_name, "planner")


def build_executor_agent(config: AppConfig, role_name: str) -> Agent:
    """创建专职执行的 Agent（拥有所有权限）。"""
    return _build_base_agent(config, role_name, "executor")


def build_router_agent(config: AppConfig) -> Agent:
    """创建用于路由决策的轻量级 Agent。"""
    provider = OpenAIProvider(
        base_url=config.openai_base_url or None,
        api_key=config.openai_api_key,
    )
    
    # 尽可能使用小模型（如果有配置的话，这里为了简单重用同一个配置，但实际中可以用更便宜的模型）
    model = OpenAIChatModel(config.model_name, provider=provider)
    
    system_prompt = (
        "你是一个专业的任务框架分析器。请只做任务理解，不要执行任务。\n"
        "你需要把用户请求整理成 TaskFrame：目标、目标实体、不可变约束、复杂度、风险和是否需要先规划。\n"
        "- task_kind 可选：qa, code_change, file_ops, shell, browser, research, unknown。\n"
        "- simple：1-2 步即可完成，且低风险。\n"
        "- complex：需要跨文件/多工具/多步骤调研、代码修改、复杂网页操作或系统性分析。\n"
        "- high risk：删除、覆盖、部署、发布、涉及密钥、登录态或不可逆操作。\n"
        "- 如果用户明确给出 URL、文件路径、函数名等目标实体，必须放入 target_entities，并在 invariants 中说明不能替换或扩大目标。\n"
        "- 简单的显式 URL 访问/打开/查看任务应为 browser + simple + requires_plan=false + allowed_autonomy=narrow。\n"
        "不确定时降低 confidence，并将 requires_plan 设为 true。"
    )
    
    return Agent(
        model,
        system_prompt=system_prompt,
        output_type=TaskFrame,
    )
