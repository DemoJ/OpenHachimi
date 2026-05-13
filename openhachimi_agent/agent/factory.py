"""Agent 构建逻辑。"""

import logging
from typing import Literal

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.content.roles import load_role_content
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools import PLANNER_TOOLSET, EXECUTOR_TOOLSET


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
        "你是一个专业的任务路由分析器。\n"
        "你需要判断用户的输入任务是 'SIMPLE_TASK' 还是 'COMPLEX_TASK'。\n"
        "- SIMPLE_TASK：简单的、1-2步即可完成的任务。例如：简单的问答、翻译、查个词、看一眼特定的网页、运行一个简单的脚本、查询系统信息等。\n"
        "- COMPLEX_TASK：复杂的、多步骤的、需要调查研究和深度规划的任务。例如：分析某人的主页、爬取数据并分析、写一个完整的项目、重构代码、修复一个疑难 Bug 等。\n"
        "请且仅回复 'SIMPLE_TASK' 或是 'COMPLEX_TASK' 这两个字符串之一，不要包含任何标点符号或其他说明。"
    )
    
    return Agent(
        model,
        system_prompt=system_prompt,
    )
