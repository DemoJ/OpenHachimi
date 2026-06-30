"""每轮易变运行时上下文:时间 / 记忆召回 / 技能索引 / TaskFrame / 执行模式提示。

设计要点
========
**v3 改造（渐进披露 / progressive disclosure）**

旧实现里 skill 召回的"决策权"被关在 router LLM 手里:
- router 看全量 skill 索引 → 输出 ``relevant_skills`` → ``_skills_block`` 按 router
  选择把命中 skill 的全文/摘要塞进 executor system prompt。
- 后果:router 漏召 → 主模型完全看不到 skill 存在;router 误召 → system prompt 被
  错 skill 全文污染(高置信度时直接几 KB 塞进去)。

新实现学习 Hermes-Agent 的渐进披露:
- Executor 永远能看到一份"技能索引"(name + description + 可选 when_to_use,按
  category 分组)。
- Skill 全文按需通过 ``get_skill_instructions(name)`` 工具拉取,**不再被动注入**。
- ``relevant_skills`` 字段已从 TaskFrame schema 中删除;``skill_direct`` 这个
  execution_mode 也一起退役。

公开 API
========
- :func:`build_system_dynamic_block(deps)` —— 给所有 agent(planner/executor)用,
  返回 ``[时间] + [TaskFrame 摘要] + [记忆召回]``。
- :func:`build_executor_extra_dynamic_block(deps)` —— 仅 executor 用,
  返回 ``[执行接力规则?] + [直接执行模式提示?] + [技能索引]``。
- :func:`build_volatile_prefix(deps)` —— 兼容旧测试的 thin wrapper。

key 设计原则:user-prompt 只承载用户原始消息 + 附件;其它一切系统级运行时
上下文走 system_prompt 动态注入。
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.content.role_filters import filter_skills_for_role, get_role_filters_from, load_all_role_bindings
from openhachimi_agent.content.skills import Skill, find_skills
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.memory.recall import build_memory_context_text


logger = logging.getLogger(__name__)

_WEEKDAY_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 技能索引里 SKILL.md 未声明 category 时的默认分组名。
_DEFAULT_SKILL_CATEGORY = "general"


def _time_block() -> str:
    """构造当前真实时间块。每次调用都重新取当前时间，保证跨天/跨会话正确。"""
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


# ── 渐进披露:技能索引(永远 executor 可见) ─────────────────────────────────


def _format_skill_index_line(skill: Skill) -> str:
    """单条 skill 索引行:`- name: description`(若 when_to_use 不空,追加说明)。"""
    cfg = skill.config
    desc = (cfg.description or "").strip()
    when = (cfg.when_to_use or "").strip()
    if desc and when:
        return f"- {cfg.name}: {desc} (触发时机: {when})"
    if desc:
        return f"- {cfg.name}: {desc}"
    return f"- {cfg.name}"


def _skill_category(skill: Skill) -> str:
    cfg = skill.config
    raw = (getattr(cfg, "category", None) or "").strip()
    return raw or _DEFAULT_SKILL_CATEGORY


def _skills_index_block(deps: AgentDeps) -> str:
    """按 category 分组渲染当前工作区的技能索引(name + description 简表)。

    始终注入(只要工作区有可读 skill)。模型自己决定要不要调
    ``get_skill_instructions(name)`` 拉某个 skill 的全文,**不再被动注入全文**。

    设计点:
    - 工作区无 skill 时返回空串,不污染 system prompt。
    - 同 category 内按 skill name 字母序稳定排序,避免 mtime 抖动让 KV cache
      命中失效。
    - 跨 category 也按字母序排,但 `general` 默认分组始终排在最前(它是"未分类"
      桶,放最前模型最容易扫到)。
    """
    skills_dirs = getattr(deps, "skills_dirs", None)
    if not skills_dirs:
        return ""
    try:
        skills = find_skills(skills_dirs)
    except Exception:  # noqa: BLE001
        logger.debug("find_skills failed when building skills index", exc_info=True)
        return ""
    if not skills:
        return ""

    # 角色级过滤:按 deps.role_name 取该角色可见的 skill 子集。
    # 与 factory._build_base_agent 的过滤共用同一份 role_filters 逻辑,保证
    # "索引里列出的 skill"与"实际注册了宏工具的 skill"一致,不会出现索引里
    # 有但调不动、或调得动但索引里看不见的错位。
    role_name = getattr(deps, "role_name", "") or ""
    if role_name:
        try:
            all_bindings = load_all_role_bindings(deps.config)
        except Exception:  # noqa: BLE001
            logger.debug("load_all_role_bindings failed in skills index", exc_info=True)
            all_bindings = {}
        binding = get_role_filters_from(all_bindings, role_name)
        skills = filter_skills_for_role(binding, skills)

    grouped: dict[str, list[Skill]] = {}
    for skill in skills:
        # disable_model_invocation 的 skill 不让模型主动唤起,所以也不列在索引里;
        # 它们仍可被 install_skill 等管理类工具触达。
        if getattr(skill.config, "disable_model_invocation", False):
            continue
        grouped.setdefault(_skill_category(skill), []).append(skill)
    if not grouped:
        return ""

    # 排序:`general` 永远先,其它按字母序。
    def _category_sort_key(cat: str) -> tuple[int, str]:
        return (0 if cat == _DEFAULT_SKILL_CATEGORY else 1, cat.lower())

    lines: list[str] = []
    for category in sorted(grouped, key=_category_sort_key):
        lines.append(f"{category}:")
        for skill in sorted(grouped[category], key=lambda s: s.config.name.lower()):
            lines.append(f"  {_format_skill_index_line(skill)}")
    catalog = "\n".join(lines)

    try:
        return render_system_prompt("runtime/skills_index", {"skills_catalog": catalog})
    except Exception:  # noqa: BLE001
        logger.debug("skills_index render failed", exc_info=True)
        return ""


# ── executor 专用动态段(只在 executor agent 上注册;planner/scheduled_executor
# 不挂这套) ──
#
# 触发矩阵:
#   - executor_todo_handoff.md  ← has_active_todos(session_state)
#   - executor_direct_mode.md   ← execution_mode == "direct"
#                                  且 has_active_todos == False
#   - skills_index.md           ← 始终注入(若工作区有可见 skill)
#
# "通用底线"(严禁假完成/伪造工具结果)随 TODO 接力一起出现 —— 这条只在有 TODO
# 时才有真正意义,简单单步任务里它本身就是噪声。


def _has_active_todos_in_state(session_state: dict[str, Any]) -> bool:
    """轻量复刻 ``service.agent_runtime.context.has_active_todos``。

    内联在这里是为了避免 ``content.runtime_context`` 反向 import ``service.*``,
    防止循环依赖。语义须与上层严格一致:既要 ``todo_state.is_active`` 为真,
    又要存在至少一个 ``status != "done"`` 的任务。如果上层语义日后扩展,
    需要同步这里。
    """
    todo_state = session_state.get("todo_state")
    if not getattr(todo_state, "is_active", False):
        return False
    tasks = getattr(todo_state, "tasks", None)
    if not isinstance(tasks, dict) or not tasks:
        return False
    return any(getattr(task, "status", None) != "done" for task in tasks.values())


def _todo_handoff_block(deps: AgentDeps) -> str:
    """有活动 TODO 才注入"执行接力规则 + 通用底线"。

    无 TODO 的简单任务(问候/单步问答/纯回答)永远不会触发这段,省掉约 400 token。
    """
    session_state = getattr(deps, "session_state", None)
    if not isinstance(session_state, dict):
        return ""
    if not _has_active_todos_in_state(session_state):
        return ""
    try:
        return render_system_prompt("runtime/executor_todo_handoff")
    except Exception:  # noqa: BLE001
        logger.debug("executor_todo_handoff render failed", exc_info=True)
        return ""


def _direct_mode_block(deps: AgentDeps) -> str:
    """无活动 TODO 时,注入"不要给低风险任务造 TODO"提示。

    有活动 TODO 时这条会和 _todo_handoff_block 的"严格按 TODO 执行"冲突,因此
    显式互斥:有 TODO → 走接力规则,无 TODO → 走 direct 提示。

    Hermes 式重构后不再依赖 router 产出的 execution_mode 字段(router 已废):
    无活动 TODO 即视为 direct 模式,注入提示让主 agent 不要给简单任务堆 TODO。
    """
    session_state = getattr(deps, "session_state", None)
    if not isinstance(session_state, dict):
        return ""
    if _has_active_todos_in_state(session_state):
        return ""
    try:
        return render_system_prompt("runtime/executor_direct_mode")
    except Exception:  # noqa: BLE001
        logger.debug("executor_direct_mode render failed", exc_info=True)
        return ""


def _workspace_hint_block(deps: AgentDeps) -> str:
    """注入"会话产物落点"软引导:模型自造的中间产物默认写到 ``.workspace/<sid>/``。
    不强制重定向 ``write_file``,只是改变模型默认选择,避免任务产物污染仓库根。
    """
    sid = getattr(deps, "session_id", "")
    if not sid:
        return ""
    try:
        return render_system_prompt("runtime/workspace_hint", {"session_id": sid})
    except Exception:  # noqa: BLE001
        logger.debug("workspace_hint render failed", exc_info=True)
        return ""


def build_executor_extra_dynamic_block(deps: AgentDeps | None) -> str:
    """构造仅 executor agent 需要的额外动态段,追加在通用动态段之后。

    设计为单独函数而非合并进 ``build_system_dynamic_block`` 的原因:planner /
    scheduled_executor 不应被 "TODO 接力 / direct-mode 偏好 / 技能索引" 这类提示词
    污染 —— 它们各自的角色提示词已经明确了职责。把 executor 专用块单独出口,
    在 ``factory.py`` 中只对 executor agent 注册。

    输出顺序:``[执行接力规则? + 通用底线] [直接执行模式提示?] [产物落点引导]
    [技能索引]``。前两块按需,后两块常驻。
    """
    if deps is None:
        return ""
    blocks: list[str] = []
    handoff = _todo_handoff_block(deps)
    if handoff:
        blocks.append(handoff)
    direct = _direct_mode_block(deps)
    if direct:
        blocks.append(direct)
    workspace_hint = _workspace_hint_block(deps)
    if workspace_hint:
        blocks.append(workspace_hint)
    skills_index = _skills_index_block(deps)
    if skills_index:
        blocks.append(skills_index)
    return "\n\n".join(blocks)


def build_system_dynamic_block(deps: AgentDeps | None) -> str:
    """构造**每轮**应该追加到 system prompt 末尾的通用动态段(所有 agent 通用)。

    输出顺序:``[时间] [记忆召回]``。任一块为空或异常则跳过;整体以空行分隔。
    当 deps 为 None / 异常时返回空字符串,保证 agent 构建期间(deps 还没准备好)
    的安全。

    Hermes 式重构后不再注入 TaskFrame 摘要 —— router 已废,主 agent 自主决定
    是否建 todo,TaskFrame 不再作为硬约束写进 system prompt。
    """
    if deps is None:
        # 即便没有 deps 也应该至少提供当前时间，便于模型在"会话开始第一轮模板渲染"
        # 等无 deps 路径下仍能感知当下时间。
        return _time_block()
    blocks: list[str] = []
    time_block = _time_block()
    if time_block:
        blocks.append(time_block)
    memory_block = _memory_block(deps)
    if memory_block:
        blocks.append(memory_block)
    return "\n\n".join(blocks)


def build_volatile_prefix(deps: AgentDeps | None) -> str:
    """兼容旧调用方与测试，但**不再被 executor/planner 直接使用**。

    渐进披露后这个函数主体被掏空(skill 全文/摘要不再被动注入),只保留记忆块
    用于必要的离线/调试场景。生产路径请改用 :func:`build_system_dynamic_block`。
    """
    if deps is None:
        return ""
    blocks: list[str] = []
    memory_block = _memory_block(deps)
    if memory_block:
        blocks.append(memory_block)
    return "\n\n".join(blocks)


__all__ = [
    "build_system_dynamic_block",
    "build_executor_extra_dynamic_block",
    "build_volatile_prefix",
]
