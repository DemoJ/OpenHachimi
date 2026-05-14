"""PydanticAI 工作区工具注册表。"""

from __future__ import annotations

from pydantic_ai import FunctionToolset

from openhachimi_agent.tools.browser import (
    browser_click, browser_get_state, browser_navigate, browser_scroll, browser_type,
    browser_list_tabs, browser_new_tab, browser_switch_tab, browser_close_tab
)
from openhachimi_agent.tools.command import command_status, run_command, send_command_input
from openhachimi_agent.tools.editing import delete_path, make_directory, replace_in_file, write_file
from openhachimi_agent.tools.filesystem import find_files, list_files, read_file, search_text
from openhachimi_agent.tools.git import git_diff, git_status
from openhachimi_agent.tools.skills import get_skill_instructions, list_skills
from openhachimi_agent.tools.web import discover_web_resources, web_fetch
from openhachimi_agent.tools.research import deep_search
from openhachimi_agent.tools.planning import create_todos, update_todo, get_todos, with_todo_reminder, with_execution_guard
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
]

_OTHER_TOOLS = [
    git_status,
    git_diff,
    web_fetch,
    discover_web_resources,
    deep_search,
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
    write_file, make_directory, replace_in_file, delete_path,
    run_command, send_command_input,
    browser_navigate, browser_click, browser_type, browser_scroll,
    browser_new_tab, browser_switch_tab, browser_close_tab,
}

_READ_ONLY_FINAL_TOOLS = []
_EXECUTION_FINAL_TOOLS = []

for _orig_tools, _middlewares in [
    (_COMMAND_TOOLS, [with_prompt_injection("commands")]),
    (_BROWSER_TOOLS, [with_prompt_injection("browser")]),
    (_SKILLS_TOOLS, [with_prompt_injection("skills")]),
    (_FILE_TOOLS, []),
    (_OTHER_TOOLS, []),
]:
    _wrapped_tools = apply_middlewares(_orig_tools, _middlewares) if _middlewares else _orig_tools
    for _orig, _wrapped in zip(_orig_tools, _wrapped_tools):
        if _orig in _MUTATION_FUNCS:
            _EXECUTION_FINAL_TOOLS.append(with_execution_ledger(with_todo_reminder(with_execution_guard(_wrapped))))
        else:
            _READ_ONLY_FINAL_TOOLS.append(with_execution_ledger(_wrapped))

# 规划器专属 Toolset：拥有只读能力、创建 TODO 能力，但没有直接破坏性执行权限
PLANNER_TOOLSET = FunctionToolset(
    tools=_READ_ONLY_FINAL_TOOLS + [with_execution_ledger(tool) for tool in _PLANNING_TOOLS]
)

# 执行器专属 Toolset：拥有所有执行权限、只读权限和更新 TODO 能力
EXECUTOR_TOOLSET = FunctionToolset(
    tools=_READ_ONLY_FINAL_TOOLS + _EXECUTION_FINAL_TOOLS + [with_execution_ledger(tool) for tool in _UPDATE_TODO_TOOL] + [with_execution_ledger(get_todos)]
)

# 保持后向兼容（部分遗留代码可能仍引用这个）
WORKSPACE_TOOLSET = FunctionToolset(
    tools=_READ_ONLY_FINAL_TOOLS + _EXECUTION_FINAL_TOOLS + [with_execution_ledger(tool) for tool in _PLANNING_TOOLS + _UPDATE_TODO_TOOL]
)
