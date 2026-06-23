"""每轮易变运行时上下文:时间 / 记忆召回 / 匹配技能。

设计要点
========
**v2 改造（修复 user-prompt 污染）**

旧实现把时间/记忆/技能拼成 volatile_prefix 后塞到每轮 user-prompt 最前面，
看似可以让 system prompt 保持稳定可缓存，但代价是：

1. user-prompt 被污染（"[系统环境] 当前真实时间"、"<memory-context>"、整段 SKILL.md
   全文…），导致 capture_turn_memories 把这些系统注入内容也当成"用户的话"抽进
   长期记忆，雪球越滚越大。
2. executor_task 模板再用 ``{{ user_message }}`` 把这一坨包了一层，每轮都出现
   一段相同的"必须遵守 TaskFrame…"前缀，纯属重复指令。
3. SKILL.md 全文无条件注入，闲聊一句话也会被打入 5-10k token 的技能定义。

新实现把"系统语义内容"全部交还给 system prompt：通过 pydantic-ai 的
``@agent.system_prompt`` 动态钩子，在每次 ``agent.run()`` 时根据 ``RunContext.deps``
重新生成时间块、记忆块、命中技能块。由于 system prompt 是按 token 前缀渐进式
命中 KV cache 的，把这些每轮都会变的内容放在 system prompt **末尾**，前面
稳定主体依然命中缓存，只损失末尾几十~几百个 token，远低于 user-prompt 内嵌
SKILL 全文造成的浪费。

公开 API
========
- :func:`build_system_dynamic_block(deps)` —— 给 ``@agent.system_prompt`` 钩子用，
  返回 ``[时间] + [TaskFrame 摘要] + [记忆召回] + [匹配技能]`` 拼成的文本块。
- :func:`build_volatile_prefix(deps)` —— 保留为兼容旧测试的 thin wrapper，仅返回
  内存/技能块（**不再含时间块**）；外部已不再调用。

key 设计原则：user-prompt 只承载用户原始消息 + 附件元数据；其它一切系统级运行时
上下文走 system_prompt 动态注入。
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

from openhachimi_agent.content.prompts import render_system_prompt
from openhachimi_agent.content.skills import find_skills
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.memory.recall import build_memory_context_text
from openhachimi_agent.tools.skills import format_skill_prompt


logger = logging.getLogger(__name__)

_WEEKDAY_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# skill_direct 注入的最低 confidence 阈值，低于该值即便 router 选中也不注入全文，
# 避免低置信匹配上来就把数千 token 的 SKILL.md 塞进 system prompt。
_SKILL_INJECTION_CONFIDENCE_THRESHOLD = 0.7

# 单轮最多注入的 skill 数量。即便 router 选了 3 个，也只取置信度最高的 2 个
# 全文注入；其余以摘要形式提示（如有）或丢弃。
_MAX_INJECTED_SKILLS = 2

# 高置信度阈值:达此置信度且 SKILL.md 体积较小时,直接全文注入(省一次工具调用)。
# 否则统一走"摘要 + 按需 get_skill_instructions 读全文"路径。
_SKILL_FULL_INLINE_CONFIDENCE = 0.85
_SKILL_FULL_INLINE_MAX_CHARS = 4000

# "信息检索原则"块触发关键词集合。
# 设计:与其在 router 端枚举更多 task_kind / execution_mode,不如直接看 router
# 已经命中的技能描述 —— 用户装的 web-search / research / browser-automation 类
# 技能,其 name/description/when_to_use 里几乎都会含下列任意一个词。命中即注入,
# 不命中(闲聊、纯本地代码任务)就不注入。中文/英文都覆盖,避免任一来源被漏掉。
_WEB_SEARCH_SKILL_KEYWORDS = (
    "search",
    "research",
    "web",
    "browser",
    "scrape",
    "crawl",
    "fetch",
    "搜索",
    "检索",
    "网搜",
    "网页",
    "调研",
    "调查",
    "研究",
    "情报",
    "资讯",
)

# task_kind 命中"研究类"任务时也注入。注意 TaskKind 字面量目前只有 "research";
# 这里多列 "investigation" 是给 router 输出超出约定时的兼容兜底,不在合法集里
# 但不会报错(`coerce_task_frame` 会回退为 unknown,这里再二次防御也无妨)。
_WEB_SEARCH_TASK_KINDS = {"research", "investigation"}


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


def _normalize_skill_match(item: Any) -> tuple[str, float] | None:
    """把 router 输出的 relevant_skills 元素归一化为 (name, confidence)。

    兼容两种历史格式：
    - 旧格式：``["skill-a", "skill-b"]`` -> 没有 confidence，按 0.8 处理（保守通过阈值）
    - 新格式：``[{"name": "skill-a", "confidence": 0.85, "reason": "..."}, ...]``
    """
    if isinstance(item, str):
        name = item.strip()
        if not name:
            return None
        return (name, 0.8)
    if isinstance(item, dict):
        name = str(item.get("name", "")).strip()
        if not name:
            return None
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        return (name, confidence)
    name = getattr(item, "name", None)
    if isinstance(name, str) and name.strip():
        try:
            confidence = float(getattr(item, "confidence", 0.8) or 0.8)
        except (TypeError, ValueError):
            confidence = 0.8
        return (name.strip(), confidence)
    return None


def _should_inject_skills(task_frame_dict: dict[str, Any]) -> bool:
    """判定本轮是否应该把命中的 skill 全文注入 system prompt。

    门控规则：
    - execution_mode == "skill_direct" 才注入（普通 qa/direct 任务不注入）
    - task_kind == "qa" + complexity == "simple" 时跳过（闲聊不需要 SKILL 全文）
    - relevant_skills 为空则不注入
    """
    execution_mode = task_frame_dict.get("execution_mode")
    if execution_mode != "skill_direct":
        return False
    task_kind = task_frame_dict.get("task_kind")
    complexity = task_frame_dict.get("complexity")
    if task_kind == "qa" and complexity == "simple":
        return False
    if not task_frame_dict.get("relevant_skills"):
        return False
    return True


def _format_skill_summary(skill: Any, confidence: float) -> str:
    """单个 skill 的摘要块:name + description + when_to_use + 读全文提示。

    用于"摘要档"注入,体积通常 200-400 字节,远小于全文(几 KB 到几十 KB)。
    模型若需要展开,可主动调 ``get_skill_instructions(skill_name)`` 读取
    完整 SKILL.md 正文。
    """
    cfg = skill.config
    when = (cfg.when_to_use or "").strip()
    desc = (cfg.description or "").strip()
    path = skill.path.resolve().as_posix()
    lines = [
        f'<skill name="{cfg.name}" confidence="{confidence:.2f}">',
        f"  description: {desc}" if desc else "",
        f"  when_to_use: {when}" if when else "",
        f"  read_full: 如需要执行流程的完整指令,调用 get_skill_instructions(\"{cfg.name}\") 获取全文。",
        f"  path: {path}",
        "</skill>",
    ]
    return "\n".join(line for line in lines if line)


def _should_inline_full(skill: Any, confidence: float) -> bool:
    """决定一个 skill 是否走"全文直注"档(否则走摘要档)。

    条件:置信度 ≥ 0.85 且 SKILL.md 体积小于 _SKILL_FULL_INLINE_MAX_CHARS。
    满足意味着"几乎肯定要用 + 文档不大",直接全文省一次 get_skill_instructions
    工具调用。其它情况一律走摘要,把按需读全文交给模型自己判断。
    """
    if confidence < _SKILL_FULL_INLINE_CONFIDENCE:
        return False
    try:
        body_len = len(skill.body or "")
    except Exception:  # noqa: BLE001
        return False
    return body_len <= _SKILL_FULL_INLINE_MAX_CHARS


def _skills_block(deps: AgentDeps) -> str:
    """匹配技能块。分级注入,避免一击撑爆 system prompt。

    门控(防"闲聊撑爆 prompt"):
    - 仅 execution_mode == "skill_direct" 才注入
    - confidence < _SKILL_INJECTION_CONFIDENCE_THRESHOLD (0.7) 的不注入
    - 单轮最多注入 _MAX_INJECTED_SKILLS (2) 个

    每个命中的 skill 选档:
    - **全文档**:置信度 ≥ 0.85 且 SKILL.md ≤ 4KB,直接嵌入完整正文。
    - **摘要档**:其它情况,只嵌入 name / description / when_to_use / path
      + 读全文提示;模型按需调 ``get_skill_instructions(name)`` 拉全文。

    这样几个大 skill 同时命中也不会爆 context: 单 skill 摘要约 200-400 字节,
    两个加起来不到 1KB。
    """
    session_state = getattr(deps, "session_state", None)
    if not isinstance(session_state, dict):
        return ""
    task_frame_dict = session_state.get("task_frame")
    if not isinstance(task_frame_dict, dict):
        return ""
    if not _should_inject_skills(task_frame_dict):
        return ""
    raw_skills = task_frame_dict.get("relevant_skills") or []
    matches: list[tuple[str, float]] = []
    for item in raw_skills:
        normalized = _normalize_skill_match(item)
        if normalized and normalized[1] >= _SKILL_INJECTION_CONFIDENCE_THRESHOLD:
            matches.append(normalized)
    if not matches:
        return ""
    matches.sort(key=lambda x: x[1], reverse=True)
    matches = matches[:_MAX_INJECTED_SKILLS]

    skills_dirs = getattr(deps, "skills_dirs", None)
    if not skills_dirs:
        return ""
    try:
        skills = find_skills(skills_dirs)
    except Exception:  # noqa: BLE001
        return ""
    skill_map = {s.config.name: s for s in skills}
    injected: list[str] = []
    injected_names: list[str] = []
    for name, confidence in matches:
        skill = skill_map.get(name)
        if skill is None:
            continue
        if _should_inline_full(skill, confidence):
            injected.append(format_skill_prompt(skill))
        else:
            injected.append(_format_skill_summary(skill, confidence))
        injected_names.append(name)
    if not injected:
        return ""
    # 把本轮被动注入过的 skill 名记到 session_state,供宏工具(build_skill_tool)
    # 在被同名宏工具调用时去重(只返回参数填充后的 body,不再重复 wrap 一份完整
    # 定义到工具结果里)。set/list 等价处理。
    if injected_names:
        try:
            session_state["injected_skill_names"] = list(injected_names)
        except Exception:  # noqa: BLE001
            pass
    return render_system_prompt("runtime/matched_skills", {"skills": "\n\n".join(injected)})


def _skill_matches_web_search(skill: Any) -> bool:
    """技能是否属于"网搜/研究类"——按 name/description/when_to_use 关键词命中。

    用户安装的网搜技能(web-search / deep-research / browser-automation 等)
    在元数据里几乎都会含下列关键词之一。规则放宽不放严:命中即注入,避免漏掉
    那些虽然内部用到网搜、但 task_kind 被 router 标成 "qa" 的边缘场景。
    """
    cfg = getattr(skill, "config", None)
    if cfg is None:
        return False
    fields = [
        getattr(cfg, "name", "") or "",
        getattr(cfg, "description", "") or "",
        getattr(cfg, "when_to_use", "") or "",
    ]
    blob = " ".join(fields).lower()
    if not blob:
        return False
    return any(keyword in blob for keyword in _WEB_SEARCH_SKILL_KEYWORDS)


def _has_web_search_skill(deps: AgentDeps, task_frame_dict: dict[str, Any]) -> bool:
    """relevant_skills 里有没有"网搜类"技能。

    只看 router 列入 relevant_skills 的技能(已经过路由判断),不扫全量技能目录,
    避免"用户装了个 web-search 但本轮根本没用"也无脑触发。
    """
    raw_skills = task_frame_dict.get("relevant_skills") or []
    matched_names: list[str] = []
    for item in raw_skills:
        normalized = _normalize_skill_match(item)
        if normalized:
            matched_names.append(normalized[0])
    if not matched_names:
        return False
    skills_dirs = getattr(deps, "skills_dirs", None)
    if not skills_dirs:
        return False
    try:
        all_skills = find_skills(skills_dirs)
    except Exception:  # noqa: BLE001
        return False
    skill_map = {s.config.name: s for s in all_skills}
    for name in matched_names:
        skill = skill_map.get(name)
        if skill is None:
            continue
        if _skill_matches_web_search(skill):
            return True
    return False


def _web_search_rules_block(deps: AgentDeps) -> str:
    """"信息检索原则"按需注入块。

    触发条件(任一命中即注入):
    - 本轮 TaskFrame.task_kind 是 "research"(或 router 偶发输出的 "investigation")
    - 本轮 router 命中的 relevant_skills 中,有 name/description/when_to_use 含
      搜索/检索/research/browser 等关键词的技能

    不命中(闲聊、纯本地代码任务、跑 lint 等)就完全不注入,省掉每轮几十 token。
    """
    session_state = getattr(deps, "session_state", None)
    if not isinstance(session_state, dict):
        return ""
    task_frame_dict = session_state.get("task_frame")
    if not isinstance(task_frame_dict, dict):
        return ""
    task_kind = task_frame_dict.get("task_kind")
    triggered = task_kind in _WEB_SEARCH_TASK_KINDS or _has_web_search_skill(deps, task_frame_dict)
    if not triggered:
        return ""
    try:
        return render_system_prompt("runtime/web_search_rules")
    except Exception:  # noqa: BLE001
        logger.debug("web_search_rules render failed", exc_info=True)
        return ""


def _task_frame_block(deps: AgentDeps) -> str:
    """把当前轮次的 TaskFrame 摘要写进 system prompt，作为执行者的硬约束。

    旧版把 TaskFrame JSON 塞在 user-prompt 里（executor_task.md 渲染），导致每轮
    user-prompt 都嵌套一段几百字的指令前缀；新版改为 system prompt 动态注入，
    user-prompt 只剩用户原话本身。
    """
    session_state = getattr(deps, "session_state", None)
    if not isinstance(session_state, dict):
        return ""
    task_frame_dict = session_state.get("task_frame")
    if not isinstance(task_frame_dict, dict):
        return ""
    payload = dict(task_frame_dict)
    known_paths = session_state.get("known_paths")
    if isinstance(known_paths, dict) and known_paths:
        payload["known_paths"] = known_paths
    try:
        rendered = render_system_prompt(
            "runtime/task_frame_block",
            {"task_frame": json.dumps(payload, ensure_ascii=False)},
        )
    except Exception:  # noqa: BLE001
        logger.debug("task_frame_block render failed", exc_info=True)
        return ""
    return rendered


# ── executor 专用动态段(只在 executor agent 上注册;planner/scheduled_executor
# 不挂这套) ──
#
# 拆分思路:把原本 executor.md 里"按场景才有用"的几大段挪出来,按本轮 session
# 状态判断是否注入,从而让简单 direct 任务的 system prompt 真正变短。
#
# 触发矩阵:
#   - executor_todo_handoff.md  ← has_active_todos(session_state)
#   - executor_direct_mode.md   ← execution_mode in {"direct", "skill_direct"}
#                                  且 has_active_todos == False
#   - executor_skill_direct.md  ← execution_mode == "skill_direct"
#                                  且 has_active_todos == False
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
    """direct / skill_direct 且无活动 TODO 时,注入"不要给低风险任务造 TODO"提示。

    有活动 TODO 时这条会和 _todo_handoff_block 的"严格按 TODO 执行"冲突,因此
    显式互斥:有 TODO → 走接力规则,无 TODO → 走 direct 提示。
    """
    session_state = getattr(deps, "session_state", None)
    if not isinstance(session_state, dict):
        return ""
    if _has_active_todos_in_state(session_state):
        return ""
    task_frame_dict = session_state.get("task_frame")
    if not isinstance(task_frame_dict, dict):
        return ""
    mode = task_frame_dict.get("execution_mode")
    if mode not in {"direct", "skill_direct"}:
        return ""
    try:
        return render_system_prompt("runtime/executor_direct_mode")
    except Exception:  # noqa: BLE001
        logger.debug("executor_direct_mode render failed", exc_info=True)
        return ""


def _skill_direct_block(deps: AgentDeps) -> str:
    """skill_direct 且无活动 TODO 时,追加"skill 是主流程,不要再宽泛探索"。

    与 _direct_mode_block 互不重复:前者讲"低风险不造 TODO",这里讲"skill 优先",
    两段语义不同,都需要时一起注入。
    """
    session_state = getattr(deps, "session_state", None)
    if not isinstance(session_state, dict):
        return ""
    if _has_active_todos_in_state(session_state):
        return ""
    task_frame_dict = session_state.get("task_frame")
    if not isinstance(task_frame_dict, dict):
        return ""
    if task_frame_dict.get("execution_mode") != "skill_direct":
        return ""
    try:
        return render_system_prompt("runtime/executor_skill_direct")
    except Exception:  # noqa: BLE001
        logger.debug("executor_skill_direct render failed", exc_info=True)
        return ""


def build_executor_extra_dynamic_block(deps: AgentDeps | None) -> str:
    """构造仅 executor agent 需要的额外动态段,追加在通用动态段之后。

    设计为单独函数而非合并进 ``build_system_dynamic_block`` 的原因:planner /
    scheduled_executor 不应被 "TODO 接力 / direct-mode 偏好" 这类提示词污染 ——
    它们各自的角色提示词已经明确了职责。把 executor 专用块单独出口,在
    ``factory.py`` 中只对 executor agent 注册。

    输出顺序:``[执行接力规则? + 通用底线] [直接执行模式提示?] [Skill 主流程?]``。
    所有块都按需,任一未触发即跳过。
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
    skill_direct = _skill_direct_block(deps)
    if skill_direct:
        blocks.append(skill_direct)
    return "\n\n".join(blocks)


def build_system_dynamic_block(deps: AgentDeps | None) -> str:
    """构造**每轮**应该追加到 system prompt 末尾的动态段。

    输出顺序：``[时间] [TaskFrame 摘要] [记忆召回] [匹配技能] [信息检索原则?]``。
    任一块为空或异常则跳过；整体以空行分隔。当 deps 为 None / 异常时返回空字符串，
    保证 agent 构建期间（deps 还没准备好）的安全。

    "信息检索原则"块按需注入（仅当本轮 task_kind 是研究类、或 router 命中了
    网搜/研究类技能时才出现），避免闲聊等非检索场景被注入无关提示词。
    """
    if deps is None:
        # 即便没有 deps 也应该至少提供当前时间，便于模型在"会话开始第一轮模板渲染"
        # 等无 deps 路径下仍能感知当下时间。
        return _time_block()
    blocks: list[str] = []
    time_block = _time_block()
    if time_block:
        blocks.append(time_block)
    task_frame_block = _task_frame_block(deps)
    if task_frame_block:
        blocks.append(task_frame_block)
    memory_block = _memory_block(deps)
    if memory_block:
        blocks.append(memory_block)
    skills_block = _skills_block(deps)
    if skills_block:
        blocks.append(skills_block)
    web_search_block = _web_search_rules_block(deps)
    if web_search_block:
        blocks.append(web_search_block)
    return "\n\n".join(blocks)


def build_volatile_prefix(deps: AgentDeps | None) -> str:
    """兼容旧调用方与测试，但**不再被 executor/planner 直接使用**。

    返回值已不再含时间块（时间已迁回 system_prompt 动态段），只保留记忆/技能
    用于必要的离线/调试场景。生产路径请改用 :func:`build_system_dynamic_block`。
    """
    if deps is None:
        return ""
    blocks: list[str] = []
    memory_block = _memory_block(deps)
    if memory_block:
        blocks.append(memory_block)
    skills_block = _skills_block(deps)
    if skills_block:
        blocks.append(skills_block)
    return "\n\n".join(blocks)


__all__ = [
    "build_system_dynamic_block",
    "build_executor_extra_dynamic_block",
    "build_volatile_prefix",
]
