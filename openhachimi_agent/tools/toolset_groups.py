"""子 agent 工具集裁剪用的命名组映射与强制剥离名单。

独立成模块(不依赖 registry.py / delegation.py)以避免循环导入:
registry.py 注册工具时引用组、subagents.py 裁剪工具集时也引用组,两者都从此导入。

设计对齐 hermes 的 ``delegate_task(toolsets=[...])`` 语义:
- 父 agent 调 delegate 时按名指定子 agent 可用的工具组(如 ["file","web"])。
- ``resolve_child_toolsets``(在 subagents.py)把组名展开成具体工具函数,
  与父 agent 实际拥有的工具做交集(防越权),再剥离 BLOCKED 工具。
- 子 agent 默认只读;写/执行类工具仅在父 agent 显式指定含该组、且父自己也持有时才暴露。
"""

from __future__ import annotations

from openhachimi_agent.tools.attachments import inspect_image
from openhachimi_agent.tools.browser import (
    browser_click, browser_extract_content, browser_get_state, browser_navigate, browser_scroll, browser_type,
    browser_list_tabs, browser_new_tab, browser_switch_tab, browser_close_tab,
)
from openhachimi_agent.tools.command import command_status, run_command, send_command_input
from openhachimi_agent.tools.editing import delete_path, make_directory, replace_in_file, write_file
from openhachimi_agent.tools.filesystem import find_files, list_files, read_file, search_text
from openhachimi_agent.tools.git import git_diff, git_status
from openhachimi_agent.tools.memory import forget_memory, list_memory, memory_stats, remember, search_memory
from openhachimi_agent.tools.planning import create_todos, get_todos, update_todo
from openhachimi_agent.tools.research import web_search
from openhachimi_agent.tools.skills import get_skill_instructions, list_skills
from openhachimi_agent.tools.web import discover_web_resources, web_fetch


# ── 命名工具组:组名 → 该组包含的工具函数列表 ──
# 子 agent 默认只读组(read/file/web/browser/memory/skills/git/planning-read);
# terminal/command 含写操作,仅在父 agent 显式指定且父也持有时才暴露。
TOOLSET_GROUPS: dict[str, list] = {
    "read": [read_file, list_files, find_files, search_text, inspect_image],
    "file": [read_file, list_files, find_files, search_text, inspect_image,
             write_file, make_directory, replace_in_file, delete_path],
    "web": [web_fetch, discover_web_resources, web_search],
    "browser": [browser_navigate, browser_click, browser_type, browser_scroll,
                browser_get_state, browser_extract_content,
                browser_list_tabs, browser_new_tab, browser_switch_tab, browser_close_tab],
    "terminal": [run_command, send_command_input, command_status],
    "git": [git_status, git_diff],
    "skills": [list_skills, get_skill_instructions],
    "memory": [search_memory, list_memory, memory_stats, remember, forget_memory],
    "planning": [get_todos, create_todos, update_todo],
}

# ── 强制剥离的工具名(对齐 hermes DELEGATE_BLOCKED_TOOLS)──
# leaf 子 agent 物理上无法做这些事,无论父 agent 指定什么组都剥离:
# - delegate_task:防无限递归委派(orchestrator 角色时由 resolve_child_toolsets 重新加回)
# - clarify_user:子 agent 不能与用户交互(hermes 同款约束)
# - remember/forget_memory:不写共享长期记忆(只读 memory 组里的 search/list/stats 仍可用)
# - create_todos/update_todo:不改父的计划状态(get_todos 只读仍可用)
# - send_message 类:无跨平台副作用(本项目暂无该工具,预留)
DELEGATE_BLOCKED_TOOL_NAMES: frozenset[str] = frozenset({
    "delegate_task",
    "delegate_research",  # 旧名,防残留
    "clarify_user",
    "remember",
    "forget_memory",
    "create_todos",
    "update_todo",
})


def group_tool_names(group_names: list[str] | None) -> set[str]:
    """把组名列表展开成工具函数名集合;None/空 → 空集(表示不限定,由调用方决定默认)。"""
    if not group_names:
        return set()
    names: set[str] = set()
    for g in group_names:
        for func in TOOLSET_GROUPS.get(g, []):
            names.add(getattr(func, "__name__", ""))
    return names
