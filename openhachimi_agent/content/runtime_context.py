"""每轮易变运行时上下文:时间 / 记忆召回 / 匹配技能。

这些内容随轮次变化(时间每秒变、召回每轮变、技能随任务变),若放在系统提示层
会击穿 provider 的 prompt 缓存。因此从系统提示迁出,改为注入到每轮用户消息前缀,
使系统提示在会话内保持稳定、可缓存。

`build_volatile_prefix` 产出的文本会被拼到 executor/planner 的用户消息最前面。
"""

from __future__ import annotations

import datetime

from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.memory.recall import build_memory_context_text
from openhachimi_agent.tools.skills import format_skill_prompt


_WEEKDAY_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _time_block() -> str:
    try:
        now = datetime.datetime.now(datetime.timezone.utc).astimezone()
        current_time = now.isoformat()
        weekday = _WEEKDAY_ZH[now.weekday()]
    except Exception:  # noqa: BLE001
        return ""
    return render_system_prompt(
        "runtime/time",
        {"current_time": current_time, "weekday": weekday},
    )


def _memory_block(deps: AgentDeps) -> str:
    config = getattr(deps, "config", None)
    if config is None:
        return ""
    memory_cfg = getattr(config, "memory", None)
    if memory_cfg is None or not getattr(memory_cfg, "enabled", False):
        return ""
    memory_context = getattr(deps, "memory_context", None)
    try:
        return build_memory_context_text(config, memory_context)
    except Exception:  # noqa: BLE001
        return ""


def _skills_block(deps: AgentDeps) -> str:
    session_state = getattr(deps, "session_state", None)
    if not isinstance(session_state, dict):
        return ""
    task_frame_dict = session_state.get("task_frame")
    if not isinstance(task_frame_dict, dict):
        return ""
    relevant_skills = task_frame_dict.get("relevant_skills", [])
    if not relevant_skills:
        return ""
    skills_dirs = getattr(deps, "skills_dirs", None)
    if not skills_dirs:
        return ""
    try:
        skills = find_skills(skills_dirs)
    except Exception:  # noqa: BLE001
        return ""
    skill_map = {s.config.name: s for s in skills}
    injected = [format_skill_prompt(skill_map[name]) for name in relevant_skills if name in skill_map]
    if not injected:
        return ""
    return render_system_prompt("runtime/matched_skills", {"skills": "\n\n".join(injected)})


def build_volatile_prefix(deps: AgentDeps) -> str:
    """构建每轮易变上下文前缀(时间 + 记忆 + 匹配技能),注入用户消息。

    各块独立,任一为空或异常则跳过;整体以空行分隔。返回空串表示无可注入内容。
    对降级/测试 mock 的 deps(可能缺属性)安全。
    """
    blocks: list[str] = []
    time_block = _time_block()
    if time_block:
        blocks.append(time_block)
    if deps is not None:
        memory_block = _memory_block(deps)
        if memory_block:
            blocks.append(memory_block)
        skills_block = _skills_block(deps)
        if skills_block:
            blocks.append(skills_block)
    return "\n\n".join(blocks)


__all__ = ["build_volatile_prefix"]
