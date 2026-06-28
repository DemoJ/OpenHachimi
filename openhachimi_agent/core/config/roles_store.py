"""角色级 skills/MCP 绑定配置的持久化读写(user/roles-config.json)。

与 mcp_store.py 同构:读容错(文件缺失/损坏回退空),写整体覆盖 + 原子替换。
绑定配置与角色提示词(user/roles/*.md)分离存储,各承一职。
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

ROLES_CONFIG_FILE_NAME = "roles-config.json"

SkillsMode = Literal["all", "selected"]
McpMode = Literal["all", "selected"]


@dataclass(frozen=True)
class RoleBindingConfig:
    """单个角色的 skills/MCP 绑定配置。

    - ``skills_mode="all"``:该角色可使用系统全部 skills(= 历史默认行为)。
    - ``skills_mode="selected"``:仅 ``selected_skills`` 列出的 skill 可见/可调用。
      selected_skills 引用的是 SKILL.md 的 ``config.name``,跨目录稳定。
    - mcp 同理,``selected_mcp_servers`` 引用 mcp-servers.json 里的 server 名。
    """

    skills_mode: SkillsMode = "all"
    selected_skills: list[str] = field(default_factory=list)
    mcp_mode: McpMode = "all"
    selected_mcp_servers: list[str] = field(default_factory=list)


def load_roles_config(user_dir: Path) -> dict[str, RoleBindingConfig]:
    """读取 user/roles-config.json。文件缺失或解析失败返回空 dict(= 全部角色默认全用)。

    每个角色的字段缺失会回退到 RoleBindingConfig 默认值(all + 空列表),
    与历史"全局生效"行为一致,保证未显式配置的角色向后兼容。
    """
    target = user_dir / ROLES_CONFIG_FILE_NAME
    try:
        if not target.exists() or not target.is_file():
            return {}
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("roles-config.json 解析失败,回退空配置: %s", exc)
        return {}

    if not isinstance(raw, dict):
        return {}
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, dict):
        return {}

    out: dict[str, RoleBindingConfig] = {}
    for name, cfg in roles_raw.items():
        if not isinstance(name, str) or not isinstance(cfg, dict):
            continue
        out[name] = _binding_from_dict(cfg)
    return out


def _binding_from_dict(cfg: dict) -> RoleBindingConfig:
    """从 JSON 对象构造 RoleBindingConfig,非法/缺失字段回退默认值。"""
    skills_mode = cfg.get("skills_mode", "all")
    mcp_mode = cfg.get("mcp_mode", "all")
    if skills_mode not in ("all", "selected"):
        skills_mode = "all"
    if mcp_mode not in ("all", "selected"):
        mcp_mode = "all"
    selected_skills = _as_str_list(cfg.get("selected_skills"))
    selected_mcp_servers = _as_str_list(cfg.get("selected_mcp_servers"))
    return RoleBindingConfig(
        skills_mode=skills_mode,  # type: ignore[arg-type]
        selected_skills=selected_skills,
        mcp_mode=mcp_mode,  # type: ignore[arg-type]
        selected_mcp_servers=selected_mcp_servers,
    )


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v).strip()]


def write_roles_config(user_dir: Path, roles: dict[str, RoleBindingConfig]) -> None:
    """整体覆盖写 user/roles-config.json,原子替换。

    roles 保留插入顺序,前端提交顺序即文件顺序。空 dict 时仍写入合法空结构,
    而非删除文件,避免"没有配置文件"与"配置了但全空"两种状态混淆。
    """
    out = {
        "roles": {
            name: {
                "skills_mode": b.skills_mode,
                "selected_skills": list(b.selected_skills),
                "mcp_mode": b.mcp_mode,
                "selected_mcp_servers": list(b.selected_mcp_servers),
            }
            for name, b in roles.items()
        }
    }
    target = user_dir / ROLES_CONFIG_FILE_NAME
    tmp = target.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, target)
        logger.info("roles-config.json rewritten, roles=%s", list(roles))
    except OSError:
        # 原子写失败时清理临时文件,避免残留 .tmp 干扰。
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def get_role_binding(
    user_dir: Path, role_name: str, roles_config: dict[str, RoleBindingConfig] | None = None
) -> RoleBindingConfig:
    """取单个角色的绑定配置;无记录返回默认(全用)。供运行期过滤复用。

    roles_config 可预读传入,避免在 factory / runtime_context 多次读盘。
    """
    if roles_config is None:
        roles_config = load_roles_config(user_dir)
    return roles_config.get(role_name, RoleBindingConfig())
