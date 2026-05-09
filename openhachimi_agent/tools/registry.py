"""PydanticAI 工作区工具注册表。"""

from __future__ import annotations

from pydantic_ai import FunctionToolset

from openhachimi_agent.tools.browser import browser_click, browser_get_state, browser_navigate, browser_scroll, browser_type
from openhachimi_agent.tools.command import command_status, run_command, send_command_input
from openhachimi_agent.tools.editing import delete_path, make_directory, replace_in_file, write_file
from openhachimi_agent.tools.filesystem import find_files, list_files, read_file, search_text
from openhachimi_agent.tools.git import git_diff, git_status
from openhachimi_agent.tools.skills import get_skill_instructions, list_skills
from openhachimi_agent.tools.web import discover_web_resources, web_fetch
from openhachimi_agent.tools.research import deep_search
from openhachimi_agent.tools.planning import create_todos, update_todo, get_todos, with_todo_reminder
from openhachimi_agent.tools.middleware import apply_middlewares, with_prompt_injection

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
    update_todo,
    get_todos,
]

# 组装横切关注点 (Middlewares)
commands_with_middleware = apply_middlewares(_COMMAND_TOOLS, [with_prompt_injection("commands")])
browser_with_middleware = apply_middlewares(_BROWSER_TOOLS, [with_prompt_injection("browser")])
skills_with_middleware = apply_middlewares(_SKILLS_TOOLS, [with_prompt_injection("skills")])

# 重新拆分读写工具，以便应用 TODO 提醒 (仅对执行类操作进行进度打断)
_ALL_APPLIED_TOOLS = commands_with_middleware + browser_with_middleware + skills_with_middleware + _FILE_TOOLS + _OTHER_TOOLS

_MUTATION_TOOL_NAMES = {
    "write_file", "make_directory", "replace_in_file", "delete_path",
    "run_command", "send_command_input",
    "browser_navigate", "browser_click", "browser_type", "browser_scroll"
}

_FINAL_TOOLS = []
for tool in _ALL_APPLIED_TOOLS:
    # Unwrap __name__ to check if it's a mutation tool
    # Because functools.wraps preserves __name__
    if tool.__name__ in _MUTATION_TOOL_NAMES:
        _FINAL_TOOLS.append(with_todo_reminder(tool))
    else:
        _FINAL_TOOLS.append(tool)

WORKSPACE_TOOLSET = FunctionToolset(
    tools=_FINAL_TOOLS + _PLANNING_TOOLS
)
