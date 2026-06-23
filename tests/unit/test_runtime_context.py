"""``runtime_context.build_system_dynamic_block`` 的按需注入回归测试。

重点覆盖 "信息检索原则" 块的开关:闲聊不注入,研究类任务/网搜技能命中时才注入。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.content.runtime_context import (
    build_executor_extra_dynamic_block,
    build_system_dynamic_block,
)
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.planning import TodoState, TodoTask


_WEB_SEARCH_HEADING = "信息检索原则"


def _make_deps(mock_config, *, task_frame: dict | None) -> AgentDeps:
    state: dict = {}
    if task_frame is not None:
        state["task_frame"] = task_frame
    return AgentDeps(
        config=mock_config,
        session_id="test-session",
        session_state=state,
    )


def test_base_prompt_no_longer_contains_retrieval_section():
    """base.md 不应再包含'信息检索原则'——已迁到 runtime/web_search_rules。"""
    base_text = load_system_prompt("base")
    assert _WEB_SEARCH_HEADING not in base_text


def test_web_search_rules_template_loads():
    """运行时模板必须存在,渲染后含完整标题。"""
    text = load_system_prompt("runtime/web_search_rules")
    assert _WEB_SEARCH_HEADING in text


def test_idle_chat_does_not_inject_web_search_rules(mock_config):
    """闲聊('你好'之类):task_kind=unknown 且 relevant_skills 空 → 不注入。"""
    deps = _make_deps(
        mock_config,
        task_frame={
            "task_kind": "unknown",
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "direct",
            "relevant_skills": [],
        },
    )
    block = build_system_dynamic_block(deps)
    assert _WEB_SEARCH_HEADING not in block


def test_qa_simple_does_not_inject_web_search_rules(mock_config):
    """简单 QA('今天几号'):普通 qa 也不应注入。"""
    deps = _make_deps(
        mock_config,
        task_frame={
            "task_kind": "qa",
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "direct",
            "relevant_skills": [],
        },
    )
    block = build_system_dynamic_block(deps)
    assert _WEB_SEARCH_HEADING not in block


def test_research_task_kind_injects_web_search_rules(mock_config):
    """task_kind=research 必须注入。"""
    deps = _make_deps(
        mock_config,
        task_frame={
            "task_kind": "research",
            "complexity": "complex",
            "risk": "low",
            "requires_plan": True,
            "execution_mode": "planned",
            "relevant_skills": [],
        },
    )
    block = build_system_dynamic_block(deps)
    assert _WEB_SEARCH_HEADING in block


def test_investigation_task_kind_injects_web_search_rules(mock_config):
    """router 偶发输出 'investigation' 也应触发(兼容兜底)。"""
    deps = _make_deps(
        mock_config,
        task_frame={
            "task_kind": "investigation",
            "complexity": "complex",
            "risk": "low",
            "requires_plan": True,
            "execution_mode": "planned",
            "relevant_skills": [],
        },
    )
    block = build_system_dynamic_block(deps)
    assert _WEB_SEARCH_HEADING in block


def _write_skill(skills_dir: Path, name: str, description: str, when_to_use: str = "") -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter_lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if when_to_use:
        frontmatter_lines.append(f"when_to_use: {when_to_use}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")
    frontmatter_lines.append(f"# {name}\n\n占位 skill body,用于运行时上下文回归测试。\n")
    (skill_dir / "SKILL.md").write_text("\n".join(frontmatter_lines), encoding="utf-8")


def _config_with_skills_dir(mock_config, skills_dir: Path):
    """AppConfig 是 frozen dataclass,这里用 dataclasses.replace 派生一份指向
    临时 skills_dir 的副本,避免直接赋值报 FrozenInstanceError。"""
    return dataclasses.replace(mock_config, skills_dirs=[skills_dir])


def test_relevant_web_search_skill_injects_web_search_rules(mock_config, tmp_path):
    """router 命中的 relevant_skills 中含网搜类技能 → 注入。"""
    skills_dir = tmp_path / "skills_root"
    _write_skill(
        skills_dir,
        name="deep-research",
        description="Multi-source web research with adversarial verification.",
        when_to_use="When the user wants a fact-checked research report.",
    )
    cfg = _config_with_skills_dir(mock_config, skills_dir)

    deps = _make_deps(
        cfg,
        task_frame={
            "task_kind": "qa",
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "skill_direct",
            "relevant_skills": [{"name": "deep-research", "confidence": 0.9}],
        },
    )
    block = build_system_dynamic_block(deps)
    assert _WEB_SEARCH_HEADING in block


def test_irrelevant_skill_does_not_inject_web_search_rules(mock_config, tmp_path):
    """router 命中的技能与网搜无关(纯本地代码工具) → 不注入。"""
    skills_dir = tmp_path / "skills_root"
    _write_skill(
        skills_dir,
        name="code-formatter",
        description="格式化本地源码文件,统一缩进与引号。",
        when_to_use="用户希望对本地代码进行格式化或风格统一时使用。",
    )
    cfg = _config_with_skills_dir(mock_config, skills_dir)

    deps = _make_deps(
        cfg,
        task_frame={
            "task_kind": "code_change",
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "skill_direct",
            "relevant_skills": [{"name": "code-formatter", "confidence": 0.9}],
        },
    )
    block = build_system_dynamic_block(deps)
    assert _WEB_SEARCH_HEADING not in block


def test_chinese_search_skill_injects_web_search_rules(mock_config, tmp_path):
    """中文描述含'搜索/检索'的技能也应触发(关键词中文覆盖)。"""
    skills_dir = tmp_path / "skills_root"
    _write_skill(
        skills_dir,
        name="zh-search",
        description="对外网内容做信息检索与摘要。",
        when_to_use="用户需要查询外部资料或最新资讯时使用。",
    )
    cfg = _config_with_skills_dir(mock_config, skills_dir)

    deps = _make_deps(
        cfg,
        task_frame={
            "task_kind": "qa",
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "skill_direct",
            "relevant_skills": [{"name": "zh-search", "confidence": 0.85}],
        },
    )
    block = build_system_dynamic_block(deps)
    assert _WEB_SEARCH_HEADING in block


def test_build_system_dynamic_block_handles_none_deps():
    """deps=None 时应至少返回时间块,不应抛异常。"""
    block = build_system_dynamic_block(None)
    # _time_block 在 import 阶段可能因测试环境时区异常而返回空串,这里不强制内容,
    # 只要求不抛异常即可。
    assert isinstance(block, str)


# ── executor 专用按需块 ───────────────────────────────────────────────────────
#
# 这一组测试覆盖方案 B:把原本恒定写在 executor.md 里的几大段(执行接力规则、
# 通用底线、直接执行模式提示、Skill 主流程提示)按 session_state 状态按需注入。

_TODO_HANDOFF_HEADING = "执行接力规则"
_DIRECT_MODE_HEADING = "直接执行模式"
_SKILL_DIRECT_HEADING = "Skill 主流程模式"


def _make_executor_deps(mock_config, *, task_frame: dict | None, todo_state=None) -> AgentDeps:
    state: dict = {}
    if task_frame is not None:
        state["task_frame"] = task_frame
    if todo_state is not None:
        state["todo_state"] = todo_state
    return AgentDeps(
        config=mock_config,
        session_id="executor-test-session",
        session_state=state,
    )


def test_executor_md_core_no_longer_contains_handoff_section():
    """executor.md 主体不应再含'执行接力规则' / 'TODO 列表'相关大段——已迁出按需。"""
    text = load_system_prompt("agents/executor")
    assert _TODO_HANDOFF_HEADING not in text
    # "直接执行模式" / "Skill 主流程模式" 标题也都迁走了
    assert _DIRECT_MODE_HEADING not in text
    assert _SKILL_DIRECT_HEADING not in text


def test_executor_md_core_keeps_tool_specific_rules():
    """工具特异性提示词(install_skill / create_delayed_task 等)仍应保留在主体里。"""
    text = load_system_prompt("agents/executor")
    assert "install_skill" in text
    assert "create_delayed_task" in text
    assert "publish_artifact" in text
    assert "research_sources" in text


def test_executor_extra_block_empty_for_idle_chat(mock_config):
    """无 TODO + execution_mode=unknown(没经过 router):executor 额外块应完全为空。"""
    deps = _make_executor_deps(mock_config, task_frame=None)
    block = build_executor_extra_dynamic_block(deps)
    assert block == ""


def test_executor_extra_block_direct_mode_only(mock_config):
    """direct 模式(简单任务)且无 TODO:仅注入'直接执行模式',不注入接力/skill。"""
    deps = _make_executor_deps(
        mock_config,
        task_frame={
            "task_kind": "qa",
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "direct",
            "relevant_skills": [],
        },
    )
    block = build_executor_extra_dynamic_block(deps)
    assert _DIRECT_MODE_HEADING in block
    assert _TODO_HANDOFF_HEADING not in block
    assert _SKILL_DIRECT_HEADING not in block


def test_executor_extra_block_skill_direct_includes_both_blocks(mock_config):
    """skill_direct + 无 TODO:同时注入'直接执行模式'(基础)与'Skill 主流程'(增强)。"""
    deps = _make_executor_deps(
        mock_config,
        task_frame={
            "task_kind": "qa",
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "skill_direct",
            "relevant_skills": [{"name": "some-skill", "confidence": 0.9}],
        },
    )
    block = build_executor_extra_dynamic_block(deps)
    assert _DIRECT_MODE_HEADING in block
    assert _SKILL_DIRECT_HEADING in block
    assert _TODO_HANDOFF_HEADING not in block


def test_executor_extra_block_active_todos_only_handoff(mock_config):
    """有活动 TODO:只注入接力规则,direct/skill_direct 块互斥让位。

    这一点防止"在 TODO 还有 pending 时却收到'不要为低风险任务造 TODO'的提示词"
    这种自相矛盾的指令——两者语义冲突,模型会无所适从。
    """
    todo_state = TodoState(
        goal="test goal",
        is_active=True,
        tasks={1: TodoTask(id=1, description="step 1", status="pending")},
    )
    deps = _make_executor_deps(
        mock_config,
        task_frame={
            "task_kind": "code_change",
            "complexity": "complex",
            "risk": "low",
            "requires_plan": True,
            "execution_mode": "planned",
            "relevant_skills": [],
        },
        todo_state=todo_state,
    )
    block = build_executor_extra_dynamic_block(deps)
    assert _TODO_HANDOFF_HEADING in block
    assert _DIRECT_MODE_HEADING not in block
    assert _SKILL_DIRECT_HEADING not in block


def test_executor_extra_block_all_todos_done_treated_as_inactive(mock_config):
    """tasks 全部 done:has_active_todos 为 False,不应再注入接力规则。"""
    todo_state = TodoState(
        goal="finished goal",
        is_active=True,
        tasks={1: TodoTask(id=1, description="done step", status="done")},
    )
    deps = _make_executor_deps(
        mock_config,
        task_frame={
            "task_kind": "qa",
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "direct",
            "relevant_skills": [],
        },
        todo_state=todo_state,
    )
    block = build_executor_extra_dynamic_block(deps)
    assert _TODO_HANDOFF_HEADING not in block
    # 既然没活动 TODO,direct 块应该正常注入
    assert _DIRECT_MODE_HEADING in block


def test_executor_extra_block_inactive_todo_state_not_handoff(mock_config):
    """todo_state.is_active=False(规划已停用):不应触发接力规则,即使 tasks 非空。"""
    todo_state = TodoState(
        goal="suspended",
        is_active=False,
        tasks={1: TodoTask(id=1, description="step", status="pending")},
    )
    deps = _make_executor_deps(
        mock_config,
        task_frame={
            "task_kind": "qa",
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "direct",
            "relevant_skills": [],
        },
        todo_state=todo_state,
    )
    block = build_executor_extra_dynamic_block(deps)
    assert _TODO_HANDOFF_HEADING not in block
    assert _DIRECT_MODE_HEADING in block


def test_executor_extra_block_planned_without_active_todos_empty(mock_config):
    """execution_mode=planned 但 TODO 还没创建(Planner 决定不规划):
    direct/skill_direct 块不该注入(模式不匹配),也没活动 TODO → 全空。"""
    deps = _make_executor_deps(
        mock_config,
        task_frame={
            "task_kind": "code_change",
            "complexity": "complex",
            "risk": "medium",
            "requires_plan": True,
            "execution_mode": "planned",
            "relevant_skills": [],
        },
    )
    block = build_executor_extra_dynamic_block(deps)
    assert block == ""


def test_executor_extra_block_none_deps_empty():
    """deps=None 路径(agent 构造期):返回空串,不应抛异常。"""
    assert build_executor_extra_dynamic_block(None) == ""
