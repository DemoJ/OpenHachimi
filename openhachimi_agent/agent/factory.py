"""Agent 构建逻辑。"""

import logging

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.content.roles import load_role_content
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
    system_prompt = load_system_prompt()
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
