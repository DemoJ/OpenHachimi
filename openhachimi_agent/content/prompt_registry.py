"""WebUI 提示词编辑暴露表(声明式元数据)。

首批通过 WebUI 设置页暴露给用户编辑的提示词清单,与 ensure_default_prompt_overrides
首次预置的副本对齐(name 列表一致)。后端 /prompts 接口据此渲染卡片、校验写入 name。

每个 PromptSpec.name 即 load_system_prompt 用的相对路径(如 "base"、"vision/default_user"),
与内置 system_prompts/ 下 .md 一一对应;user/system_prompts/<同名>.md 覆盖之。

不在本表内的提示词(agents/*、runtime/*、memory/* 等内部协议类)不通过 WebUI 暴露,
但仍可用手动放 user/system_prompts/ 下同名 .md 的方式覆盖(加载机制相同)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    name: str                       # load_system_prompt 相对路径
    title: str                      # UI 标题
    description: str                # UI 说明
    has_template_vars: bool         # 是否含 {{ }} 占位符(仅用于 UI 提示)
    restart_note: str = ""          # 生效说明


# 首批暴露的 3 个;name 必须与 loading._DEFAULT_PROMPT_OVERRIDES 完全一致。
# has_template_vars 仅 context/summary 为真(其 {{ current_date }} 实际不经渲染加载,见
# content/summary.py:97 用 load_system_prompt 非 render;UI 仍标注以提示对照内置)。
PROMPTS: tuple[PromptSpec, ...] = (
    PromptSpec(
        name="base",
        title="基础人格",
        description="主模型系统提示词,定义角色人格、核心原则与工具使用规范。整段替换会覆盖内置规范,建议在末尾追加你的要求而非整体重写。",
        has_template_vars=False,
        restart_note="改后新会话生效;运行中的旧会话仍用其构建时的旧提示词。",
    ),
    PromptSpec(
        name="vision/default_user",
        title="图片识别提示词",
        description="辅助视觉模型识别图片时使用的用户提示词,决定识别产出的详略与重点方向(OCR 向 / 描述向)。",
        has_template_vars=False,
        restart_note="下次图片识别生效。",
    ),
    PromptSpec(
        name="context/summary",
        title="上下文压缩摘要",
        description="长对话上下文压缩摘要的系统提示词。含 {{ current_date }} 占位符(本提示词不经渲染加载,占位符仅作文本提示,可保留以对照内置)。",
        has_template_vars=True,
        restart_note="新会话生效。",
    ),
)


def get_prompt_spec(name: str) -> PromptSpec | None:
    """按 name 查元数据;不在首批表内返回 None(后端据此拒绝写入)。"""
    for spec in PROMPTS:
        if spec.name == name:
            return spec
    return None