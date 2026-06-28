"""角色级 skills/MCP 过滤。

集中实现"按角色绑定配置筛选可见 skills / MCP toolset"的逻辑,供 agent 构建路径
(``agent.factory._build_base_agent``)与运行期技能索引块
(``content.runtime_context._skills_index_block``)复用,避免两处各写一遍。

放在 ``content`` 下而非 ``service`` 下,是为了不引入 ``content`` 对 ``service``
的反向依赖(与 ``runtime_context`` 内联 ``find_skills`` 的解耦思路一致)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openhachimi_agent.core.config import RoleBindingConfig, get_role_binding, load_roles_config

if TYPE_CHECKING:
    from openhachimi_agent.core.config import AppConfig
    from openhachimi_agent.content.skills import Skill


def get_role_filters(config: "AppConfig", role_name: str) -> RoleBindingConfig:
    """取指定角色的绑定配置;无记录返回默认(全用)。

    运行期调用频繁,建议调用方按需缓存(如 agent_cache 的 mtime 失效周期内)。
    """
    return get_role_binding(config.user_dir, role_name)


def get_role_filters_from(
    roles_config: dict[str, RoleBindingConfig], role_name: str
) -> RoleBindingConfig:
    """从已预读的 roles_config 取角色过滤集,避免反复读盘。

    runtime_context 一次 run 内多个块复用同一份 roles_config 时用这个。
    """
    return roles_config.get(role_name, RoleBindingConfig())


def filter_skills_for_role(
    binding: RoleBindingConfig, skills: "list[Skill]"
) -> "list[Skill]":
    """按角色绑定筛选 skills。

    - ``skills_mode="all"``:原样返回(= 历史全局行为)。
    - ``skills_mode="selected"``:仅保留 ``config.name`` 命中 ``selected_skills`` 的 skill。
      以 name 而非 source_path 匹配——name 跨目录稳定,且 find_skills 本就按 name 去重。
    """
    if binding.skills_mode != "selected":
        return list(skills)
    wanted = set(binding.selected_skills)
    if not wanted:
        return []
    return [s for s in skills if s.config.name in wanted]


def filter_mcp_toolsets_for_role(
    binding: RoleBindingConfig,
    named_toolsets: "list[tuple[str, object]]",
) -> "list[tuple[str, object]]":
    """按角色绑定筛选"带名"的 MCP toolsets。

    入参为 ``[(server_name, toolset), ...]``(见 ``tools.mcp.load_mcp_toolsets``)。

    - ``mcp_mode="all"``:原样返回(全部连接的 server 都对模型可见)。
    - ``mcp_mode="selected"``:仅保留 server 名命中 ``selected_mcp_servers`` 的项。
      未命中的 server 仍保持连接(连接由 service 全局单栈管理),只是不加入该角色的 agent toolsets。
    """
    if binding.mcp_mode != "selected":
        return list(named_toolsets)
    wanted = set(binding.selected_mcp_servers)
    if not wanted:
        return []
    return [pair for pair in named_toolsets if pair[0] in wanted]


def load_all_role_bindings(config: "AppConfig") -> dict[str, RoleBindingConfig]:
    """预读全部角色绑定配置,供一次 run 内多块复用。"""
    return load_roles_config(config.user_dir)
