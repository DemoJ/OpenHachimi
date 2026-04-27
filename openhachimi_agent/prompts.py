"""共享提示词加载。"""

from pathlib import Path


def load_system_prompt(prompts_dir: Path, name: str = "base") -> str:
    """加载所有角色共用的系统提示词。"""
    prompt_path = prompts_dir / f"{name}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"未找到共享系统提示词文件：{prompt_path}")

    content = prompt_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"共享系统提示词文件为空：{prompt_path.name}")

    return content
