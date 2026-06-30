"""Agent 构建与依赖 mtime 检测。

`AgentService` 持有 `_agents` 字典与 `_agent_dependency_mtime_cache`,这里提供
纯函数形式的检测与构建逻辑,便于单测与复用。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from openhachimi_agent.agent.factory import (
    build_main_agent,
    build_subagent_agent,
)
from openhachimi_agent.core.config import ROLES_CONFIG_FILE_NAME, AppConfig


logger = logging.getLogger(__name__)

AGENT_DEPENDENCY_MTIME_TTL_SECONDS = 2.0


def compute_dependency_mtime(
    config: AppConfig,
    role_name: str,
    cache: tuple[float, float] | None,
) -> tuple[float, tuple[float, float]]:
    """计算 Agent 依赖文件(角色定义、SKILL.md)的最大 mtime。

    缓存命中(TTL 内)直接返回旧值,否则重新扫描。返回 (mtime, new_cache)。
    """
    now = time.monotonic()
    if cache is not None:
        checked_at, cached_mtime = cache
        if now - checked_at < AGENT_DEPENDENCY_MTIME_TTL_SECONDS:
            return cached_mtime, cache

    current_mtime = 0.0
    paths_to_check = [
        config.roles_dir / f"{role_name}.md",
        # roles-config.json 改动也要让 agent 缓存失效——它决定该角色的 skills/MCP
        # 可见集,改了不重建会用到旧绑定。
        config.user_dir / ROLES_CONFIG_FILE_NAME,
    ]
    try:
        for path in paths_to_check:
            if path.exists() and path.is_file():
                current_mtime = max(current_mtime, path.stat().st_mtime)

        for skills_dir in config.skills_dirs:
            if not skills_dir.exists() or not skills_dir.is_dir():
                continue
            for skill_file in skills_dir.rglob("SKILL.md"):
                if skill_file.is_file():
                    current_mtime = max(current_mtime, skill_file.stat().st_mtime)
    except Exception as exc:
        logger.debug("Failed to check mtime for agent dependencies: %s", exc)

    return current_mtime, (now, current_mtime)


def build_agent_by_type(
    config: AppConfig,
    role_name: str,
    agent_type: str,
    mcp_toolsets: list,
    run_mode: str = "interactive",
):
    """根据 agent_type 分发到对应工厂函数。

    Hermes 式重构后只剩两类:subagent(委派子 agent,零记忆 str 输出)和
    main(单一主 agent,scheduled 模式复用 main + 注入 scheduled prompt)。
    """
    if agent_type == "subagent":
        return build_subagent_agent(config, role_name, mcp_toolsets=mcp_toolsets)
    return build_main_agent(config, role_name, mcp_toolsets=mcp_toolsets, run_mode=run_mode)


def get_or_build_agent(
    agents: dict[str, tuple[Any, float]],
    config: AppConfig,
    role_name: str,
    agent_type: str,
    mcp_toolsets: list,
    current_mtime: float,
    run_mode: str = "interactive",
):
    """读写 `agents` 缓存,过期则重建。返回 Agent 实例。

    缓存键含 run_mode,使 interactive / scheduled 的 main agent 各占一条缓存
    (scheduled 多注入了 scheduled_executor prompt,不能复用 interactive 实例)。
    """
    cache_key = f"{role_name}:{agent_type}:{run_mode}"
    cached = agents.get(cache_key)
    if cached is None or cached[1] < current_mtime:
        if cached is not None:
            logger.info(
                "rebuilding %s agent due to dependency updates role=%s",
                agent_type,
                role_name,
            )
        agent = build_agent_by_type(config, role_name, agent_type, mcp_toolsets, run_mode=run_mode)
        agents[cache_key] = (agent, current_mtime)
    return agents[cache_key][0]
