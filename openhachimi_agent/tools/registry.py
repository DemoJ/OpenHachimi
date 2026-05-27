"""PydanticAI 工作区工具注册表。"""

from __future__ import annotations

from pydantic_ai import FunctionToolset

from openhachimi_agent.tools.artifacts import publish_artifact
from openhachimi_agent.tools.attachments import inspect_image
from openhachimi_agent.tools.browser import (
    browser_click, browser_get_state, browser_navigate, browser_scroll, browser_type,
    browser_list_tabs, browser_new_tab, browser_switch_tab, browser_close_tab
)
from openhachimi_agent.tools.command import command_status, run_command, send_command_input
from openhachimi_agent.tools.editing import delete_path, make_directory, replace_in_file, write_file
from openhachimi_agent.tools.filesystem import find_files, list_files, read_file, search_text
from openhachimi_agent.tools.git import git_diff, git_status
from openhachimi_agent.tools.skills import get_skill_instructions, list_skills, install_skill
from openhachimi_agent.tools.web import discover_web_resources, web_fetch
from openhachimi_agent.tools.research import web_search
from openhachimi_agent.tools.scheduler import create_delayed_task, create_scheduled_task
from openhachimi_agent.tools.planning import create_todos, update_todo, get_todos, with_todo_reminder, with_execution_guard
from openhachimi_agent.tools.memory import forget_memory, list_memory, memory_stats, remember, search_memory
from openhachimi_agent.tools.middleware import apply_middlewares, with_prompt_injection
from openhachimi_agent.agent.execution import with_execution_ledger

_COMMAND_TOOLS = [
    run_command,
    send_command_input,
    command_status,
]

_BROWSER_TOOLS = [
    browser_navigate,
    browser_click,
    browser_type,
    browser_scroll,
    browser_get_state,
    browser_list_tabs,
    browser_new_tab,
    browser_switch_tab,
    browser_close_tab,
]

_SKILLS_TOOLS = [
    list_skills,
    get_skill_instructions,
]

_FILE_TOOLS = [
    write_file,
    make_directory,
    replace_in_file,
    delete_path,
    list_files,
    find_files,
    search_text,
    read_file,
    inspect_image,
    publish_artifact,
]

_OTHER_TOOLS = [
    git_status,
    git_diff,
    web_fetch,
    discover_web_resources,
    web_search,
]

_MEMORY_READ_TOOLS = [
    search_memory,
    list_memory,
    memory_stats,
]

_MEMORY_MUTATION_TOOLS = [
    remember,
    forget_memory,
]

_SCHEDULER_TOOLS = [
    create_delayed_task,
    create_scheduled_task,
]

_PLANNING_TOOLS = [
    create_todos,
    get_todos,
]

_UPDATE_TODO_TOOL = [
    update_todo,
]

# 组装横切关注点 (Middlewares) 和 TODO 提醒
_MUTATION_FUNCS = {
    write_file, make_directory, replace_in_file, delete_path, publish_artifact,
    run_command, send_command_input,
    browser_navigate, browser_click, browser_type, browser_scroll,
    browser_new_tab, browser_switch_tab, browser_close_tab,
    install_skill,
    remember, forget_memory,
}

_READ_ONLY_FINAL_TOOLS = []
_EXECUTION_FINAL_TOOLS = []

for _orig_tools, _middlewares in [
    (_COMMAND_TOOLS, [with_prompt_injection("commands")]),
    (_BROWSER_TOOLS, [with_prompt_injection("browser")]),
    (_SKILLS_TOOLS, []),
    (_FILE_TOOLS, []),
    (_OTHER_TOOLS, []),
]:
    _wrapped_tools = apply_middlewares(_orig_tools, _middlewares) if _middlewares else _orig_tools
    for _orig, _wrapped in zip(_orig_tools, _wrapped_tools):
        if _orig in _MUTATION_FUNCS:
            _EXECUTION_FINAL_TOOLS.append(with_execution_ledger(with_todo_reminder(with_execution_guard(_wrapped))))
        else:
            _READ_ONLY_FINAL_TOOLS.append(with_execution_ledger(_wrapped))

for _tool in _MEMORY_READ_TOOLS:
    _READ_ONLY_FINAL_TOOLS.append(with_execution_ledger(_tool))

for _tool in _MEMORY_MUTATION_TOOLS:
    _EXECUTION_FINAL_TOOLS.append(with_execution_ledger(with_todo_reminder(with_execution_guard(_tool))))

for _tool in _SCHEDULER_TOOLS:
    _EXECUTION_FINAL_TOOLS.append(with_execution_ledger(_tool))

# ── Planner 专用工具集 ──
# Planner 是纯规划者：只需要本地只读工具来理解项目上下文，然后基于对
# Executor 工具能力的了解来制定计划。不应该有任何网络执行类工具（web_search、
# web_fetch、browser_* 等），否则 Planner 会"提前调研"导致目标漂移。
_PLANNER_CONTEXT_FUNCS = {
    list_files, find_files, search_text, read_file,   # 本地文件只读
    git_status, git_diff,                              # Git 只读
    list_skills, get_skill_instructions,               # 技能查询
    search_memory, list_memory, memory_stats,           # 长期记忆查询
}
_planner_allowed_names = {f.__name__ for f in _PLANNER_CONTEXT_FUNCS}
_PLANNER_CONTEXT_TOOLS = [
    tool for tool in _READ_ONLY_FINAL_TOOLS
    if getattr(tool, "__name__", "") in _planner_allowed_names
]

PLANNER_TOOLSET = FunctionToolset(
    tools=_PLANNER_CONTEXT_TOOLS + [with_execution_ledger(tool) for tool in _PLANNING_TOOLS],
    max_retries=3,
)

# ── Executor 专用工具集 ──
# Executor 拥有所有执行权限、只读权限和更新 TODO 能力
EXECUTOR_TOOLSET = FunctionToolset(
    tools=_READ_ONLY_FINAL_TOOLS + _EXECUTION_FINAL_TOOLS + [with_execution_ledger(tool) for tool in _UPDATE_TODO_TOOL] + [with_execution_ledger(get_todos)],
    max_retries=3,
)

# 保持后向兼容（部分遗留代码可能仍引用这个）
WORKSPACE_TOOLSET = FunctionToolset(
    tools=_READ_ONLY_FINAL_TOOLS + _EXECUTION_FINAL_TOOLS + [with_execution_ledger(tool) for tool in _PLANNING_TOOLS + _UPDATE_TODO_TOOL],
    max_retries=3,
)
