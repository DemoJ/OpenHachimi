"""Agent 构建逻辑。"""

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.config import AppConfig
from openhachimi_agent.prompts import load_system_prompt
from openhachimi_agent.roles import load_role_content
from openhachimi_agent.tools import WORKSPACE_TOOLSET


def build_agent(config: AppConfig, role_name: str) -> Agent:
    """根据指定角色创建 Agent。"""
    system_prompt = load_system_prompt(config.prompts_dir)
    role_content = load_role_content(config.roles_dir, role_name)
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
