"""版本内置系统提示词加载与渲染。

支持用户目录覆盖:user/system_prompts/<同名 relative path>.md 若存在且非空,
优先于内置包内 .md。覆盖目录由 :func:`set_prompts_override_dir` 在启动期注入
(``load_config`` 确定 user_dir 后调用);未注入时仅读内置,行为与改造前一致。

单一事实来源:提示词始终以 .md 文件存在,不再走 config.yaml 的多行文本字段。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path, PurePosixPath


SYSTEM_PROMPTS_PACKAGE = "openhachimi_agent.system_prompts"
_TEMPLATE_VAR_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")


# 用户目录下的覆盖根目录({user_dir}/system_prompts);由 load_config 注入。
# None = 未注入,仅读内置(如 import 期 / 单元测试 / 未走完整启动流程时)。
_OVERRIDE_DIR: Path | None = None


def set_prompts_override_dir(user_dir: Path | None) -> None:
    """注入覆盖根目录,启用 user/system_prompts/.md 覆盖机制。

    传 None 关闭覆盖。调用方仅 :func:`load_config` 启动期一次;避免在运行期反复改,
    否则覆盖态会随调用点时机而抖动。
    """
    global _OVERRIDE_DIR
    _OVERRIDE_DIR = (Path(user_dir) / "system_prompts") if user_dir else None


def get_prompts_override_dir() -> Path | None:
    """当前覆盖根目录(主要供测试与诊断查看)。"""
    return _OVERRIDE_DIR


def resolve_override_path(name: str) -> Path | None:
    """返回 name 对应的覆盖文件绝对路径(不判断是否存在)。

    未注入覆盖目录时返回 None。复用 _prompt_path_parts 校验,拒绝 ../ 等非法名。
    供 exists 判断、写回、删除等操作统一取路径,避免各调用方各自拼路径。
    """
    if _OVERRIDE_DIR is None:
        return None
    parts = _prompt_path_parts(name)
    return _OVERRIDE_DIR.joinpath(*parts)


def is_overridden(name: str) -> bool:
    """该提示词当前是否有非空的用户覆盖(等价于 _load_override 返回非 None)。"""
    return _load_override(name) is not None


def write_override(name: str, content: str) -> None:
    """把 content 写入 user/system_prompts/<name>.md(父目录自动创建)。

    content 原样写入(不做 strip),保留用户输入的空行/缩进;空 content 会写成空文件,
    加载时被 _load_override 视作"未接管"自动回退内置。若语义上要回退内置,请用 delete_override。
    """
    if _OVERRIDE_DIR is None:
        raise RuntimeError("覆盖目录未注入,无法写回提示词(未走完整启动流程?)")
    target = resolve_override_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def delete_override(name: str) -> bool:
    """删除覆盖文件,回退内置。存在且已删返回 True,不存在返回 False(语义上等同已回退)。"""
    target = resolve_override_path(name)
    if target is None or not target.is_file():
        return False
    target.unlink()
    return True


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


def _load_override(name: str) -> str | None:
    """读覆盖文件;存在且 .strip() 非空返回内容,否则 None(回退内置)。

    空覆盖文件视作"未接管"自动回退内置,避免空白 .md 导致空提示词进模型。
    """
    if _OVERRIDE_DIR is None:
        return None
    parts = _prompt_path_parts(name)  # 复用同一校验,拒绝 ../ 等非法名
    override_file = _OVERRIDE_DIR.joinpath(*parts)
    if not override_file.is_file():
        return None
    content = override_file.read_text(encoding="utf-8").strip()
    return content or None


def load_system_prompt(name: str = "base") -> str:
    """加载系统提示词:优先用户目录覆盖,回退内置包内 .md。"""
    override = _load_override(name)
    if override is not None:
        return override

    prompt_file = files(SYSTEM_PROMPTS_PACKAGE).joinpath(*_prompt_path_parts(name))
    if not prompt_file.is_file():
        raise FileNotFoundError(f"未找到内置系统提示词文件：{name}.md")

    content = prompt_file.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"内置系统提示词文件为空：{name}.md")

    return content


def render_system_prompt(name: str, variables: Mapping[str, object] | None = None) -> str:
    """加载并渲染系统提示词模板。

    模板变量格式为 ``{{ variable_name }}``。所有模板变量必须显式提供,
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