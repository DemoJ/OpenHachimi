"""PydanticAI 工作区工具注册表。"""

from __future__ import annotations

from pydantic_ai import FunctionToolset

from openhachimi_agent.tools.command import command_status, run_command, send_command_input
from openhachimi_agent.tools.editing import delete_path, make_directory, replace_in_file, write_file
from openhachimi_agent.tools.filesystem import find_files, list_files, read_file, search_text
from openhachimi_agent.tools.git import git_diff, git_status
from openhachimi_agent.tools.skills import get_skill_instructions, list_skills


WORKSPACE_TOOLSET = FunctionToolset(
    tools=[
        list_files,
        find_files,
        search_text,
        read_file,
        write_file,
        make_directory,
        replace_in_file,
        delete_path,
        run_command,
        command_status,
        send_command_input,
        git_status,
        git_diff,
        list_skills,
        get_skill_instructions,
    ]
)
