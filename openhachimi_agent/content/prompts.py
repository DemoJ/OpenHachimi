"""版本内置系统提示词加载与渲染。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from importlib.resources import files
from pathlib import PurePosixPath


SYSTEM_PROMPTS_PACKAGE = "openhachimi_agent.system_prompts"
_TEMPLATE_VAR_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


def _prompt_path_parts(name: str) -> tuple[str, ...]:
    normalized = str(name).replace("\\", "/").strip()
    if not normalized:
        raise ValueError("系统提示词名称不能为空。")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"系统提示词名称非法：{name}")
    if path.suffix:
        raise ValueError("系统提示词名称不应包含文件扩展名。")
    return (*path.parts[:-1], f"{path.name}.md")


def load_system_prompt(name: str = "base") -> str:
    """加载随当前版本发布的系统提示词。"""
    prompt_file = files(SYSTEM_PROMPTS_PACKAGE).joinpath(*_prompt_path_parts(name))
    if not prompt_file.is_file():
        raise FileNotFoundError(f"未找到内置系统提示词文件：{name}.md")

    content = prompt_file.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"内置系统提示词文件为空：{name}.md")

    return content


def render_system_prompt(name: str, variables: Mapping[str, object] | None = None) -> str:
    """加载并渲染系统提示词模板。

    模板变量格式为 ``{{ variable_name }}``。所有模板变量必须显式提供，
    以避免未渲染的提示词进入模型上下文。
    """
    template = load_system_prompt(name)
    values = dict(variables or {})
    required = set(_TEMPLATE_VAR_PATTERN.findall(template))
    missing = sorted(required - set(values))
    if missing:
        missing_text = "、".join(missing)
        raise ValueError(f"系统提示词模板 {name} 缺少变量：{missing_text}")

    def replace(match: re.Match[str]) -> str:
        return str(values[match.group(1)])

    return _TEMPLATE_VAR_PATTERN.sub(replace, template)
