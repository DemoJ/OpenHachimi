"""Agent 构建逻辑。"""

import logging

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.content.roles import load_role_content
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.tools import WORKSPACE_TOOLSET


logger = logging.getLogger(__name__)


def build_agent(config: AppConfig, role_name: str) -> Agent:
    """根据指定角色创建 Agent。"""
    if not config.openai_api_key:
        raise ValueError("未配置 llm.api_key，请先在 user/config.yaml 中填写 API Key。")

    logger.info(
        "building agent role=%s model=%s base_url_configured=%s",
        role_name,
        config.model_name,
        bool(config.openai_base_url),
    )
    import datetime
    
    current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    system_prompt = f"[系统环境] 当前真实时间: {current_time}\n\n" + load_system_prompt()
    
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

    return Agent(
        OpenAIChatModel(config.model_name, provider=provider),
        system_prompt=system_prompt,
        instructions=role_content,
        deps_type=AppConfig,
        toolsets=[WORKSPACE_TOOLSET],
        defer_model_check=True,
    )
