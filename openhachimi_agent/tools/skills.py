"""Skills tools.

This module provides tools for the agent to discover and read
Claude Skills defined in the workspace.
"""

import os
import shutil
import tempfile
import urllib.request
import zipfile
import tarfile
import subprocess
import filecmp
import stat
from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.content.skills import find_skills, parse_skill, Skill
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps


_SKILL_DOWNLOAD_TIMEOUT_SECONDS = 60
_SKILL_GIT_CLONE_TIMEOUT_SECONDS = 120


def _download_url(source_url: str, download_path: Path) -> None:
    """Download a URL to disk with an explicit timeout."""
    with urllib.request.urlopen(source_url, timeout=_SKILL_DOWNLOAD_TIMEOUT_SECONDS) as response:
        with download_path.open("wb") as output_file:
            shutil.copyfileobj(response, output_file)


def _resolve_archive_target(base_dir: Path, member_name: str) -> Path:
    """Return the resolved extraction target, rejecting paths outside base_dir."""
    target_path = (base_dir / member_name).resolve()
    if not target_path.is_relative_to(base_dir.resolve()):
        raise ValueError(f"Unsafe archive member path: {member_name}")
    return target_path


def _safe_extract_zip(zip_ref: zipfile.ZipFile, dest_dir: Path) -> None:
    """Extract zip entries after validating each destination path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for member in zip_ref.infolist():
        _resolve_archive_target(dest_dir, member.filename)
    zip_ref.extractall(dest_dir)


def _safe_extract_tar(tar_ref: tarfile.TarFile, dest_dir: Path) -> None:
    """Extract regular tar entries after blocking traversal and unsafe link/device types."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for member in tar_ref.getmembers():
        _resolve_archive_target(dest_dir, member.name)
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"Unsafe tar member type: {member.name}")
        if member.isdir() or member.isfile():
            continue
        if not stat.S_ISREG(member.mode) and not stat.S_ISDIR(member.mode):
            raise ValueError(f"Unsupported tar member type: {member.name}")
    tar_ref.extractall(dest_dir)


def install_skill(ctx: RunContext[AgentDeps], source_path_or_url: str) -> str:
    """Installs or updates a Claude Skill into this OpenHachimi project's user/skills directory.

    Prefer this tool whenever the user asks to install, add, update, import, or fetch
    a skill from a GitHub/Git URL, a zip/tar download URL, or a local skill directory.
    This tool is the default installation path for third-party skills:
    <current OpenHachimi project>/user/skills/<skill-name>.

    Do not install into ~/.agents/skills or paths.external_skills_dir unless the user
    explicitly asks to configure that external directory instead of installing a
    project skill. If a particular skill documents a required command-based update
    flow, use that flow only when the user asks for it or this source-based installer
    is not suitable.

    Args:
        source_path_or_url: The Git repository URL, a zip/tar download URL, or a local directory containing the skill.

    Returns:
        A success message with the installation result, or an error message if it fails.
    """
    user_skills_dir = ctx.deps.base_dir / "user" / "skills"
    user_skills_dir.mkdir(parents=True, exist_ok=True)
    
    tmp_dir = Path(tempfile.mkdtemp(prefix="openhachimi_skill_"))
    try:
        # 1. Fetch source
        repo_dir = tmp_dir / "repo"
        if source_path_or_url.startswith(("http://", "https://")):
            if source_path_or_url.endswith(".git") or "github.com" in source_path_or_url:
                try:
                    subprocess.run(
                        ["git", "clone", "--depth", "1", source_path_or_url, str(repo_dir)],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=_SKILL_GIT_CLONE_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired:
                    return f"Failed to git clone {source_path_or_url}: operation timed out after {_SKILL_GIT_CLONE_TIMEOUT_SECONDS} seconds"
                except subprocess.CalledProcessError as e:
                    return f"Failed to git clone {source_path_or_url}: {e.stderr}"
            else:
                download_path = tmp_dir / "downloaded_archive"
                try:
                    _download_url(source_path_or_url, download_path)
                    if source_path_or_url.endswith(".zip"):
                        with zipfile.ZipFile(download_path, 'r') as zip_ref:
                            _safe_extract_zip(zip_ref, repo_dir)
                    elif source_path_or_url.endswith((".tar.gz", ".tgz", ".tar")):
                        with tarfile.open(download_path, 'r') as tar_ref:
                            _safe_extract_tar(tar_ref, repo_dir)
                    else:
                        return "Unsupported URL format. Provide a .git URL or a .zip/.tar archive URL."
                except Exception as e:
                    return f"Failed to download or extract archive from {source_path_or_url}: {e}"
        else:
            # Local path
            local_source = Path(source_path_or_url).expanduser().resolve()
            if not local_source.exists():
                return f"Local path {local_source} does not exist."
            try:
                if local_source.is_dir():
                    shutil.copytree(local_source, repo_dir, dirs_exist_ok=True)
                else:
                    return "Local path must be a directory containing SKILL.md."
            except Exception as e:
                return f"Failed to copy local path {local_source}: {e}"

        # 2. Find SKILL.md
        skill_md_paths = list(repo_dir.rglob("SKILL.md")) + list(repo_dir.rglob("skill.md"))
        if not skill_md_paths:
            return f"No SKILL.md found in {source_path_or_url}. Ensure the source is a valid skill."
        
        target_skill_file = skill_md_paths[0]
        skill_dir = target_skill_file.parent
        
        # 3. Parse skill to get name
        parsed_skill = parse_skill(target_skill_file)
        if not parsed_skill:
            return f"Found {target_skill_file.name} but failed to parse YAML frontmatter. Invalid skill format."
            
        skill_name = parsed_skill.config.name
        if not skill_name:
            return "Skill config is missing 'name'."
            
        # 4. Cleanup .git before comparison/copy
        git_dir = skill_dir / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)

        # 5. Check conflicts and update
        dest_dir = user_skills_dir / skill_name
        
        if dest_dir.exists():
            # Check diff recursively
            def are_dirs_same(dir1, dir2):
                cmp = filecmp.dircmp(dir1, dir2)
                if cmp.left_only or cmp.right_only or cmp.diff_files:
                    return False
                for common_dir in cmp.common_dirs:
                    if not are_dirs_same(Path(dir1)/common_dir, Path(dir2)/common_dir):
                        return False
                return True

            if are_dirs_same(skill_dir, dest_dir):
                return f"Skill '{skill_name}' is already installed and is up-to-date."
            else:
                shutil.rmtree(dest_dir, ignore_errors=True)
                shutil.copytree(skill_dir, dest_dir)
                return f"Skill '{skill_name}' has been successfully updated."
        else:
            shutil.copytree(skill_dir, dest_dir)
            return f"Skill '{skill_name}' has been successfully installed."

    except Exception as e:
        return f"Unexpected error during skill installation: {e}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def list_skills(ctx: RunContext[AgentDeps]) -> str:
    """Lists available Claude Skills for the current project.
    
    Returns:
        A formatted string listing the name, description, when to use, 
        and the file path for each discovered skill.
    """
    skills = find_skills(ctx.deps.skills_dirs)
    if not skills:
        return "No skills found in the current project."
        
    result = ["Available skills for this project:"]
    for skill in skills:
        entry = f"- Name: {skill.config.name}\n  Path: {skill.path}\n  Description: {skill.config.description}"
        if skill.config.when_to_use:
            entry += f"\n  When to use: {skill.config.when_to_use}"
        result.append(entry)
        
    return "\n\n".join(result)


def get_skill_instructions(ctx: RunContext[AgentDeps], skill_name: str) -> str:
    """Gets the specific markdown instructions for a named skill.
    
    Args:
        skill_name: The exact name of the skill to read (e.g. 'explain-code').
        
    Returns:
        The markdown body of the skill, or an error message if not found.
    """
    skills = find_skills(ctx.deps.skills_dirs)
    
    for skill in skills:
        if skill.config.name == skill_name:
            if skill.config.disable_model_invocation:
                return f"Skill '{skill_name}' is marked with disable_model_invocation=true. You should not run this skill directly."
            return format_skill_prompt(skill)

    return f"Skill '{skill_name}' not found. Please check available skills using list_skills."

from pydantic import create_model
from typing import Callable


def format_skill_prompt(skill: Skill, body: str | None = None, *, execution_intro: str | None = None) -> str:
    """Format skill instructions with path metadata for reading bundled resources."""
    skill_path = skill.path.resolve().as_posix()
    skill_root = skill.path.parent.resolve().as_posix()
    content = skill.body if body is None else body
    intro = f"{execution_intro}\n" if execution_intro else ""
    return (
        f"<skill name=\"{skill.config.name}\" skill_root=\"{skill_root}\" path=\"{skill_path}\">\n"
        f"{intro}"
        "[Skill Metadata]\n"
        f"- skill_path: {skill_path}\n"
        f"- skill_root: {skill_root}\n\n"
        "[Path Note]\n"
        "read_file、list_files、find_files 和 search_text 的相对路径仍相对于当前项目工作区根目录，"
        "不会自动相对于本 skill 目录解析。\n"
        "如果本 skill 需要读取自身附带的参考文件、模板、示例或脚本，请将 skill 文档中的相对路径"
        "与上方 skill_root 拼接成绝对路径后再调用文件工具。\n\n"
        f"{content}\n"
        "</skill>"
    )


def build_skill_tool(skill: Skill) -> Callable:
    """Dynamically builds a pydantic_ai Tool function for a skill with arguments."""
    arg_fields = {arg: (str, ...) for arg in skill.config.arguments or []}
    ArgsModel = create_model(f"{skill.config.name.replace('-', '_').capitalize()}Args", **arg_fields)
    
    def dynamic_skill_tool(ctx: RunContext[AgentDeps], args: ArgsModel) -> str:
        body = skill.body
        for arg in skill.config.arguments or []:
            val = getattr(args, arg)
            # Use simple string replacement for {{arg}}
            body = body.replace(f"{{{{{arg}}}}}", str(val))
        execution_intro = (
            f"【Skill Execution: {skill.config.name}】\n"
            "Treat this skill as the primary workflow for the current task. "
            "Execute the instructions directly; avoid broad repository exploration, repeated skill lookup, "
            "or re-checking already successful file paths unless an input is missing, a tool fails, "
            "or the user explicitly asks for verification."
        )
        return format_skill_prompt(skill, body, execution_intro=execution_intro)
    
    dynamic_skill_tool.__name__ = f"skill_{skill.config.name.replace('-', '_')}"
    dynamic_skill_tool.__doc__ = f"Executes the {skill.config.name} skill. {skill.config.description}"
    
    return dynamic_skill_tool
