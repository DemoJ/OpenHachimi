"""角色配置加载。"""

from pathlib import Path


def list_role_names(roles_dir: Path) -> list[str]:
    """列出当前 `roles` 目录下所有可用角色名称。"""
    if not roles_dir.exists():
        return []

    return sorted(file.stem for file in roles_dir.glob("*.md") if file.is_file())


def load_role_content(roles_dir: Path, role_name: str) -> str:
    """从 Markdown 文件中加载角色配置内容。"""
    role_path = roles_dir / f"{role_name}.md"
    if not role_path.exists():
        available_roles = "、".join(list_role_names(roles_dir)) or "无"
        raise FileNotFoundError(
            f"未找到角色配置：{role_name}。当前可用角色：{available_roles}"
        )

    content = role_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"角色配置文件为空：{role_path.name}")

    return content

