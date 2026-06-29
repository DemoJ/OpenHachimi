"""``runtime_context.build_system_dynamic_block`` 与 ``build_executor_extra_dynamic_block``
的按需注入回归测试。

重点覆盖:
- executor 专用动态段:TODO 接力 / direct-mode / 技能索引按本轮 session 状态触发。
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


_TODO_HANDOFF_HEADING = "执行接力规则"
_DIRECT_MODE_HEADING = "直接执行模式"
_SKILLS_INDEX_HEADING = "Skills（可用技能索引）"


def _make_deps(mock_config, *, task_frame: dict | None) -> AgentDeps:
    state: dict = {}
    if task_frame is not None:
        state["task_frame"] = task_frame
    return AgentDeps(
        config=mock_config,
        session_id="test-session",
        session_state=state,
    )


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


def _write_skill(
    skills_dir: Path,
    name: str,
    description: str,
    when_to_use: str = "",
    category: str | None = None,
) -> None:
    """在测试临时目录里造一个最小可解析的 SKILL.md。"""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter_lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if when_to_use:
        frontmatter_lines.append(f"when_to_use: {when_to_use}")
    if category:
        frontmatter_lines.append(f"category: {category}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")
    frontmatter_lines.append(f"# {name}\n\n占位 skill body,用于运行时上下文回归测试。\n")
    (skill_dir / "SKILL.md").write_text("\n".join(frontmatter_lines), encoding="utf-8")


def _config_with_skills_dir(mock_config, skills_dir: Path):
    """AppConfig 是 frozen dataclass,这里用 dataclasses.replace 派生一份指向
    临时 skills_dir 的副本,避免直接赋值报 FrozenInstanceError。"""
    return dataclasses.replace(mock_config, skills_dirs=[skills_dir])


# ── 模板存在性 ─────────────────────────────────


def test_skills_index_template_loads():
    """技能索引模板必须存在,且要求 skills_catalog 变量。"""
    text = load_system_prompt("runtime/skills_index")
    assert _SKILLS_INDEX_HEADING in text
    assert "{{ skills_catalog }}" in text


# ── build_system_dynamic_block 兜底 ─────────────────────────────────────────


def test_build_system_dynamic_block_handles_none_deps():
    """deps=None 时应至少返回时间块,不应抛异常。"""
    block = build_system_dynamic_block(None)
    assert isinstance(block, str)


# ── executor 专用按需块 ───────────────────────────────────────────────────────


def test_executor_md_core_no_longer_contains_handoff_section():
    """executor.md 主体不应再含'执行接力规则' / '直接执行模式'相关大段——已迁出按需。"""
    text = load_system_prompt("agents/executor")
    assert _TODO_HANDOFF_HEADING not in text
    assert _DIRECT_MODE_HEADING not in text


def test_executor_md_core_keeps_tool_specific_rules():
    """工具特异性提示词(install_skill / create_delayed_task 等)仍应保留在主体里。"""
    text = load_system_prompt("agents/executor")
    assert "install_skill" in text
    assert "create_delayed_task" in text
    assert "publish_artifact" in text
    assert "delegate_task" in text
    # research_sources 已合并删除,executor.md 不再提它
    assert "research_sources" not in text


def test_executor_md_does_not_duplicate_skills_index_guidance():
    """executor.md 主体不应再讲'如何使用 skill'——这部分指引由 skills_index.md
    在按需注入索引时一起承载,避免重复。

    工作区无 skill 时,索引段不出现,这套指引也跟着消失,语义自洽。
    """
    text = load_system_prompt("agents/executor")
    assert "get_skill_instructions" not in text
    # "技能（skill）使用方式" 这一节也应不存在
    assert "技能（skill）使用方式" not in text


def test_skills_index_template_contains_progressive_disclosure_guidance():
    """skills_index 模板既是数据 (skills_catalog) 也是指令——必须告诉模型
    按需调 get_skill_instructions 拉全文,不要凭空臆造技能名。"""
    text = load_system_prompt("runtime/skills_index")
    assert "get_skill_instructions" in text
    assert "不要凭空臆造" in text


def test_executor_extra_block_empty_for_idle_chat_no_skills(mock_config):
    """无 TODO + 无 task_frame + 无 skill:executor 额外块仅含产物落点引导。

    mock_config.skills_dirs 默认指向 ``tmp_path / .claude / skills``(conftest 设置),
    那个目录不存在,所以 find_skills 返回空,索引块不注入。
    产物落点引导(workspace_hint)是常驻块,只要 deps.session_id 非空就出现。
    """
    deps = _make_executor_deps(mock_config, task_frame=None)
    block = build_executor_extra_dynamic_block(deps)
    # TODO 接力块、direct mode 块、skills index 块都不应出现
    assert "执行接力规则" not in block
    assert "直接执行模式" not in block
    assert "技能索引" not in block
    # 仅产物落点引导常驻
    assert "中间产物落点" in block
    assert ".workspace/" in block


def test_executor_extra_block_direct_mode_only(mock_config):
    """direct 模式(简单任务)且无 TODO + 无 skill:仅注入'直接执行模式'。"""
    deps = _make_executor_deps(
        mock_config,
        task_frame={
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "direct",
        },
    )
    block = build_executor_extra_dynamic_block(deps)
    assert _DIRECT_MODE_HEADING in block
    assert _TODO_HANDOFF_HEADING not in block
    # 工作区无 skill → 不应出现技能索引
    assert _SKILLS_INDEX_HEADING not in block


def test_executor_extra_block_active_todos_only_handoff(mock_config):
    """有活动 TODO:只注入接力规则,direct 块互斥让位。"""
    todo_state = TodoState(
        goal="test goal",
        is_active=True,
        tasks={1: TodoTask(id=1, description="step 1", status="pending")},
    )
    deps = _make_executor_deps(
        mock_config,
        task_frame={
            "complexity": "complex",
            "risk": "low",
            "requires_plan": True,
            "execution_mode": "planned",
        },
        todo_state=todo_state,
    )
    block = build_executor_extra_dynamic_block(deps)
    assert _TODO_HANDOFF_HEADING in block
    assert _DIRECT_MODE_HEADING not in block


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
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "direct",
        },
        todo_state=todo_state,
    )
    block = build_executor_extra_dynamic_block(deps)
    assert _TODO_HANDOFF_HEADING not in block
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
            "complexity": "simple",
            "risk": "low",
            "requires_plan": False,
            "execution_mode": "direct",
        },
        todo_state=todo_state,
    )
    block = build_executor_extra_dynamic_block(deps)
    assert _TODO_HANDOFF_HEADING not in block
    assert _DIRECT_MODE_HEADING in block


def test_executor_extra_block_planned_without_active_todos_empty(mock_config):
    """execution_mode=planned 但 TODO 还没创建:direct 块不该注入(模式不匹配)。"""
    deps = _make_executor_deps(
        mock_config,
        task_frame={
            "complexity": "complex",
            "risk": "medium",
            "requires_plan": True,
            "execution_mode": "planned",
        },
    )
    block = build_executor_extra_dynamic_block(deps)
    # planned 模式不触发 direct 块;工作区也没 skill;只保留产物落点常驻引导。
    assert "执行接力规则" not in block
    assert "直接执行模式" not in block
    assert "技能索引" not in block
    assert "中间产物落点" in block


def test_executor_extra_block_none_deps_empty():
    """deps=None 路径(agent 构造期):返回空串,不应抛异常。"""
    assert build_executor_extra_dynamic_block(None) == ""


# ── 技能索引(渐进披露的核心) ───────────────────────────────────────────────


def test_skills_index_single_skill_default_category(mock_config, tmp_path):
    """单个无 category 的 skill:索引含 general 桶 + skill 行。"""
    skills_dir = tmp_path / "skills_root"
    _write_skill(
        skills_dir,
        name="demo",
        description="Demo skill for testing.",
    )
    cfg = _config_with_skills_dir(mock_config, skills_dir)
    deps = _make_executor_deps(cfg, task_frame=None)
    block = build_executor_extra_dynamic_block(deps)
    assert _SKILLS_INDEX_HEADING in block
    assert "general:" in block
    assert "- demo: Demo skill for testing." in block
    # 索引只给摘要,不应包含 skill body
    assert "占位 skill body" not in block


def test_skills_index_multiple_categories_grouped_general_first(mock_config, tmp_path):
    """多 category:`general` 永远在最前,其它按字母序。"""
    skills_dir = tmp_path / "skills_root"
    _write_skill(skills_dir, name="alpha-skill", description="Alpha skill.", category="alpha")
    _write_skill(skills_dir, name="zeta-skill", description="Zeta skill.", category="zeta")
    _write_skill(skills_dir, name="plain-skill", description="No category here.")  # → general
    cfg = _config_with_skills_dir(mock_config, skills_dir)
    deps = _make_executor_deps(cfg, task_frame=None)
    block = build_executor_extra_dynamic_block(deps)

    # 顺序断言:general 在 alpha 前,alpha 在 zeta 前
    pos_general = block.find("general:")
    pos_alpha = block.find("alpha:")
    pos_zeta = block.find("zeta:")
    assert 0 <= pos_general < pos_alpha < pos_zeta


def test_skills_index_includes_when_to_use_when_present(mock_config, tmp_path):
    """SKILL.md 写了 when_to_use 时,索引行应一并展示(触发时机)。"""
    skills_dir = tmp_path / "skills_root"
    _write_skill(
        skills_dir,
        name="trigger-demo",
        description="Demo with explicit trigger.",
        when_to_use="When user explicitly invokes it.",
    )
    cfg = _config_with_skills_dir(mock_config, skills_dir)
    deps = _make_executor_deps(cfg, task_frame=None)
    block = build_executor_extra_dynamic_block(deps)
    assert "trigger-demo" in block
    assert "(触发时机: When user explicitly invokes it.)" in block


def test_skills_index_empty_workspace_no_block(mock_config, tmp_path):
    """工作区无 skill:索引块完全不注入(连标题都不出现)。"""
    skills_dir = tmp_path / "empty_skills"
    skills_dir.mkdir()
    cfg = _config_with_skills_dir(mock_config, skills_dir)
    deps = _make_executor_deps(cfg, task_frame=None)
    block = build_executor_extra_dynamic_block(deps)
    assert _SKILLS_INDEX_HEADING not in block


def test_skills_index_excludes_disable_model_invocation(mock_config, tmp_path):
    """``disable_model_invocation=true`` 的 skill 不应出现在模型可见的索引里。"""
    skills_dir = tmp_path / "skills_root"
    skill_dir = skills_dir / "private"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: private\n"
        "description: Private skill not meant for model invocation.\n"
        "disable-model-invocation: true\n"
        "---\n\n"
        "internal body\n",
        encoding="utf-8",
    )
    # 同时放一条正常 skill,验证不是把整个索引一起吞掉
    _write_skill(skills_dir, name="visible", description="Should be visible.")
    cfg = _config_with_skills_dir(mock_config, skills_dir)
    deps = _make_executor_deps(cfg, task_frame=None)
    block = build_executor_extra_dynamic_block(deps)
    assert "visible" in block
    assert "private" not in block


def test_skills_index_does_not_include_skill_body(mock_config, tmp_path):
    """关键不变量:索引只是 name+description 简表,**不应包含 SKILL.md 正文**。"""
    skills_dir = tmp_path / "skills_root"
    _write_skill(skills_dir, name="demo", description="Demo skill.")
    cfg = _config_with_skills_dir(mock_config, skills_dir)
    deps = _make_executor_deps(cfg, task_frame=None)
    block = build_executor_extra_dynamic_block(deps)
    # SKILL.md 正文里有这串占位文字,索引层绝对不能漏出去
    assert "占位 skill body" not in block
    # 也不应包含 SKILL 全文标识
    assert "<skill " not in block


def test_skills_index_dedupes_same_name_across_dirs(mock_config, tmp_path):
    """两个 skills_dirs(主目录 + 外部扩展目录)装了同名 skill 时,索引里应只出现一次。

    回归用例:之前 ``find_skills`` 不去重,会让同一 skill 在 system prompt 索引
    里出现两遍,白白浪费 token。现在 ``find_skills`` 已在 content/skills.py 层
    按 ``config.name`` first-wins 去重,这条测试守护这个不变量。
    """
    import dataclasses as _dc

    primary = tmp_path / "primary"
    external = tmp_path / "external"
    _write_skill(primary, name="shared-skill", description="Primary copy.")
    _write_skill(external, name="shared-skill", description="External copy.")

    cfg = _dc.replace(mock_config, skills_dirs=[primary, external])
    deps = _make_executor_deps(cfg, task_frame=None)
    block = build_executor_extra_dynamic_block(deps)
    # 严格只出现一次
    assert block.count("- shared-skill:") == 1
    # 保留的是 primary 目录的版本(first-wins)
    assert "Primary copy." in block
    assert "External copy." not in block
