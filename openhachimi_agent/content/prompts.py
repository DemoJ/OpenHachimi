"""版本内置系统提示词加载。"""

from importlib.resources import files


SYSTEM_PROMPTS_PACKAGE = "openhachimi_agent.system_prompts"


def load_system_prompt(name: str = "base") -> str:
    """加载随当前版本发布的系统提示词。"""
    prompt_file = files(SYSTEM_PROMPTS_PACKAGE).joinpath(f"{name}.md")
    if not prompt_file.is_file():
        raise FileNotFoundError(f"未找到内置系统提示词文件：{name}.md")

    content = prompt_file.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"内置系统提示词文件为空：{name}.md")

    return content
