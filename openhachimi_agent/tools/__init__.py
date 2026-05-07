"""工作区工具包。"""

from openhachimi_agent.tools.browser import browser_click, browser_get_state, browser_navigate, browser_scroll, browser_type
from openhachimi_agent.tools.command import run_command
from openhachimi_agent.tools.editing import make_directory, replace_in_file, write_file
from openhachimi_agent.tools.filesystem import find_files, list_files, read_file, search_text
from openhachimi_agent.tools.git import git_diff, git_status
from openhachimi_agent.tools.registry import WORKSPACE_TOOLSET
from openhachimi_agent.tools.web import discover_web_resources, web_fetch

__all__ = [
    "WORKSPACE_TOOLSET",
    "list_files",
    "find_files",
    "search_text",
    "read_file",
    "write_file",
    "make_directory",
    "replace_in_file",
    "run_command",
    "git_status",
    "git_diff",
    "discover_web_resources",
    "web_fetch",
    "browser_navigate",
    "browser_get_state",
    "browser_click",
    "browser_type",
    "browser_scroll",
]
