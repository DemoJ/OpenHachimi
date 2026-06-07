"""角色配置加载。"""

from pathlib import Path

from openhachimi_agent.core.identifiers import safe_role_file_path, validate_role_name


def list_role_names(roles_dir: Path) -> list[str]:
    """列出当前 `roles` 目录下所有可用角色名称。"""
    if not roles_dir.exists():
        return []

    roles: list[str] = []
    root = roles_dir.resolve()
    for file in roles_dir.glob("*.md"):
        if not file.is_file():
            continue
        try:
            file.resolve().relative_to(root)
            validate_role_name(file.stem)
        except ValueError:
            continue
        roles.append(file.stem)
    return sorted(roles)


def load_role_content(roles_dir: Path, role_name: str) -> str:
    """从 Markdown 文件中加载角色配置内容。"""
    role = validate_role_name(role_name)
    role_path = safe_role_file_path(roles_dir, role)
    if not role_path.exists() or not role_path.is_file():
        available_roles = "、".join(list_role_names(roles_dir)) or "无"
        raise FileNotFoundError(
            f"未找到角色配置：{role}。当前可用角色：{available_roles}"
        )

    content = role_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"角色配置文件为空：{role_path.name}")

    return content
