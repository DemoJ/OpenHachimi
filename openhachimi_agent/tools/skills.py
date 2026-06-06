"""Skills tools.

This module provides tools for the agent to discover and read
Claude Skills defined in the workspace.
"""

import filecmp
import hashlib
import hmac
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Callable
from urllib.parse import urlsplit

from pydantic import create_model
from pydantic_ai import RunContext

from openhachimi_agent.content.skills import Skill, find_skills, parse_skill
from openhachimi_agent.core.deps import AgentDeps


_SKILL_DOWNLOAD_TIMEOUT_SECONDS = 60
_SKILL_GIT_CLONE_TIMEOUT_SECONDS = 120
_SKILL_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_SKILL_MAX_ARCHIVE_MEMBERS = 1000
_SKILL_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_SKILL_MAX_FILE_BYTES = 20 * 1024 * 1024
_SKILL_MAX_INSTALLED_FILES = 1000
_SKILL_MAX_INSTALLED_BYTES = 100 * 1024 * 1024
_SKILL_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")
_WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _is_windows_reserved_name(name: str) -> bool:
    stem = PureWindowsPath(name).stem.upper()
    return stem in _WINDOWS_DEVICE_NAMES


def _validate_skill_name(skill_name: str) -> str:
    """Validate a skill name before using it as an installation directory."""
    if not isinstance(skill_name, str):
        raise ValueError("Skill name must be a string.")
    candidate = skill_name.strip()
    raw_path = Path(candidate)
    win_path = PureWindowsPath(candidate)
    if not candidate:
        raise ValueError("Skill config is missing 'name'.")
    if candidate in {".", ".."}:
        raise ValueError(f"Invalid skill name {skill_name!r}: reserved path segment.")
    if raw_path.is_absolute() or win_path.is_absolute() or len(raw_path.parts) > 1 or len(win_path.parts) > 1:
        raise ValueError(f"Invalid skill name {skill_name!r}: path separators are not allowed.")
    if not _SKILL_NAME_PATTERN.fullmatch(candidate):
        raise ValueError(
            f"Invalid skill name {skill_name!r}: use 1-64 characters from letters, numbers, '.', '_' and '-', "
            "and start with a letter or number."
        )
    if _is_windows_reserved_name(candidate):
        raise ValueError(f"Invalid skill name {skill_name!r}: reserved Windows device name.")
    return candidate


def _resolve_skill_dest(user_skills_dir: Path, skill_name: str) -> Path:
    """Resolve a skill install destination and ensure it remains under user_skills_dir."""
    safe_name = _validate_skill_name(skill_name)
    base = user_skills_dir.resolve()
    dest_dir = (base / safe_name).resolve()
    if not dest_dir.is_relative_to(base):
        raise ValueError(f"Invalid skill name {skill_name!r}: installation path escapes user skills directory.")
    return dest_dir


def _validate_sha256(expected_sha256: str | None) -> str | None:
    if expected_sha256 is None:
        return None
    expected = expected_sha256.strip().lower()
    if not _SHA256_PATTERN.fullmatch(expected):
        raise ValueError("expected_sha256 must be a 64-character hexadecimal SHA-256 digest.")
    return expected


def _download_url(
    source_url: str,
    download_path: Path,
    *,
    max_bytes: int = _SKILL_MAX_DOWNLOAD_BYTES,
    expected_sha256: str | None = None,
) -> str:
    """Download a URL to disk with timeout, size limit, and optional SHA-256 verification."""
    expected_digest = _validate_sha256(expected_sha256)
    hasher = hashlib.sha256()
    total_bytes = 0

    with urllib.request.urlopen(source_url, timeout=_SKILL_DOWNLOAD_TIMEOUT_SECONDS) as response:
        content_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > max_bytes:
                raise ValueError(f"Download is too large: {declared_size} bytes exceeds limit {max_bytes} bytes")

        with download_path.open("wb") as output_file:
            while True:
                chunk = response.read(_SKILL_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValueError(f"Download exceeded maximum size of {max_bytes} bytes")
                hasher.update(chunk)
                output_file.write(chunk)

    digest = hasher.hexdigest()
    if expected_digest and not hmac.compare_digest(digest, expected_digest):
        raise ValueError(f"SHA-256 mismatch: expected {expected_digest}, got {digest}")
    return digest


def _resolve_archive_target(base_dir: Path, member_name: str) -> Path:
    """Return the resolved extraction target, rejecting paths outside base_dir."""
    target_path = (base_dir / member_name).resolve()
    if not target_path.is_relative_to(base_dir.resolve()):
        raise ValueError(f"Unsafe archive member path: {member_name}")
    return target_path


def _zip_member_is_symlink(member: zipfile.ZipInfo) -> bool:
    mode = member.external_attr >> 16
    return stat.S_ISLNK(mode)


def _safe_extract_zip(
    zip_ref: zipfile.ZipFile,
    dest_dir: Path,
    *,
    max_members: int = _SKILL_MAX_ARCHIVE_MEMBERS,
    max_total_uncompressed_bytes: int = _SKILL_MAX_UNCOMPRESSED_BYTES,
    max_file_bytes: int = _SKILL_MAX_FILE_BYTES,
) -> None:
    """Extract zip entries after validating paths, types, and resource limits."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    members = zip_ref.infolist()
    if len(members) > max_members:
        raise ValueError(f"Archive contains too many entries: {len(members)} exceeds limit {max_members}")

    total_size = 0
    for member in members:
        _resolve_archive_target(dest_dir, member.filename)
        if _zip_member_is_symlink(member):
            raise ValueError(f"Unsafe zip member type: {member.filename}")
        if member.is_dir():
            continue
        if member.file_size > max_file_bytes:
            raise ValueError(f"Archive member is too large: {member.filename}")
        total_size += member.file_size
        if total_size > max_total_uncompressed_bytes:
            raise ValueError(f"Archive uncompressed size exceeds limit {max_total_uncompressed_bytes} bytes")
    zip_ref.extractall(dest_dir)


def _safe_extract_tar(
    tar_ref: tarfile.TarFile,
    dest_dir: Path,
    *,
    max_members: int = _SKILL_MAX_ARCHIVE_MEMBERS,
    max_total_uncompressed_bytes: int = _SKILL_MAX_UNCOMPRESSED_BYTES,
    max_file_bytes: int = _SKILL_MAX_FILE_BYTES,
) -> None:
    """Extract regular tar entries after blocking traversal, unsafe types, and oversized archives."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    members = tar_ref.getmembers()
    if len(members) > max_members:
        raise ValueError(f"Archive contains too many entries: {len(members)} exceeds limit {max_members}")

    total_size = 0
    for member in members:
        _resolve_archive_target(dest_dir, member.name)
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"Unsafe tar member type: {member.name}")
        if member.isdir():
            continue
        if member.isfile():
            if member.size > max_file_bytes:
                raise ValueError(f"Archive member is too large: {member.name}")
            total_size += member.size
            if total_size > max_total_uncompressed_bytes:
                raise ValueError(f"Archive uncompressed size exceeds limit {max_total_uncompressed_bytes} bytes")
            continue
        if not stat.S_ISREG(member.mode) and not stat.S_ISDIR(member.mode):
            raise ValueError(f"Unsupported tar member type: {member.name}")
    tar_ref.extractall(dest_dir)


def _validate_tree_limits(
    root_dir: Path,
    *,
    max_files: int = _SKILL_MAX_INSTALLED_FILES,
    max_total_bytes: int = _SKILL_MAX_INSTALLED_BYTES,
    max_file_bytes: int = _SKILL_MAX_FILE_BYTES,
) -> None:
    """Validate the final skill tree before installation."""
    total_files = 0
    total_bytes = 0
    for item in root_dir.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"Skill contains unsupported symlink: {item.relative_to(root_dir)}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise ValueError(f"Skill contains unsupported file type: {item.relative_to(root_dir)}")
        size = item.stat().st_size
        if size > max_file_bytes:
            raise ValueError(f"Skill file is too large: {item.relative_to(root_dir)}")
        total_files += 1
        if total_files > max_files:
            raise ValueError(f"Skill contains too many files: {total_files} exceeds limit {max_files}")
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise ValueError(f"Skill size exceeds limit {max_total_bytes} bytes")


def _are_dirs_same(dir1: Path, dir2: Path) -> bool:
    cmp = filecmp.dircmp(dir1, dir2)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    for common_dir in cmp.common_dirs:
        if not _are_dirs_same(dir1 / common_dir, dir2 / common_dir):
            return False
    return True


def _copytree(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest)


def _install_or_update_skill_dir(skill_dir: Path, dest_dir: Path, skill_name: str) -> str:
    """Install or update a skill using staging and backup rollback."""
    parent = dest_dir.parent
    staging_dir = parent / f".{skill_name}.staging.{uuid.uuid4().hex}"
    backup_dir = parent / f".{skill_name}.backup.{uuid.uuid4().hex}"

    try:
        _copytree(skill_dir, staging_dir)
        _validate_tree_limits(staging_dir)

        if dest_dir.exists() and _are_dirs_same(staging_dir, dest_dir):
            shutil.rmtree(staging_dir)
            return f"Skill '{skill_name}' is already installed and is up-to-date."

        if not dest_dir.exists():
            staging_dir.rename(dest_dir)
            return f"Skill '{skill_name}' has been successfully installed."

        dest_dir.rename(backup_dir)
        try:
            staging_dir.rename(dest_dir)
        except Exception as exc:
            if dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
            try:
                backup_dir.rename(dest_dir)
                return f"Failed to update skill '{skill_name}': {exc}. Existing installation was restored."
            except Exception as rollback_exc:
                return (
                    f"Failed to update skill '{skill_name}': {exc}. Rollback also failed: {rollback_exc}. "
                    f"Backup remains at {backup_dir}."
                )

        try:
            shutil.rmtree(backup_dir)
        except Exception as cleanup_exc:
            return (
                f"Skill '{skill_name}' has been successfully updated, but failed to remove backup "
                f"{backup_dir}: {cleanup_exc}"
            )
        return f"Skill '{skill_name}' has been successfully updated."
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def _is_remote_url(source: str) -> bool:
    return urlsplit(source).scheme.lower() in {"http", "https"}


def _has_url_scheme(source: str) -> bool:
    """Return True for real URL schemes, but not Windows drive-letter paths."""
    scheme = urlsplit(source).scheme.lower()
    return bool(scheme) and not (len(scheme) == 1 and PureWindowsPath(source).drive)


def _is_git_source(source_url: str) -> bool:
    parsed = urlsplit(source_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return path.endswith(".git") or host == "github.com" or host.endswith(".github.com")


def install_skill(
    ctx: RunContext[AgentDeps],
    source_path_or_url: str,
    expected_sha256: str | None = None,
    allow_http: bool = False,
) -> str:
    """Installs or updates a Claude Skill into this OpenHachimi project's user/skills directory.

    Prefer this tool whenever the user asks to install, add, update, import, or fetch
    a skill from a GitHub/Git URL, a zip/tar download URL, or a local skill directory.
    Remote HTTP sources are rejected by default; use HTTPS or explicitly set
    allow_http=True only for trusted networks. Archive download URLs may provide
    expected_sha256 for integrity verification.

    Args:
        source_path_or_url: The Git repository URL, a zip/tar download URL, or a local directory containing the skill.
        expected_sha256: Optional SHA-256 digest for downloaded archive URLs.
        allow_http: Explicitly allow insecure http:// sources. Defaults to False.

    Returns:
        A success message with the installation result, or an error message if it fails.
    """
    user_skills_dir = ctx.deps.base_dir / "user" / "skills"
    user_skills_dir.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.mkdtemp(prefix="openhachimi_skill_"))
    try:
        # 1. Fetch source
        repo_dir = tmp_dir / "repo"
        parsed_source = urlsplit(source_path_or_url)
        scheme = parsed_source.scheme.lower() if _has_url_scheme(source_path_or_url) else ""
        if scheme:
            if scheme not in {"http", "https"}:
                return "Unsupported URL scheme. Use HTTPS, a .git URL over HTTPS, or a local skill directory."
            if scheme == "http" and not allow_http:
                return "HTTP skill sources are disabled by default. Use HTTPS or pass allow_http=True only for trusted networks."

            if _is_git_source(source_path_or_url):
                if expected_sha256 is not None:
                    return "expected_sha256 is only supported for archive download URLs, not Git sources."
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
                    _download_url(source_path_or_url, download_path, expected_sha256=expected_sha256)
                    path = parsed_source.path.lower()
                    if path.endswith(".zip"):
                        with zipfile.ZipFile(download_path, "r") as zip_ref:
                            _safe_extract_zip(zip_ref, repo_dir)
                    elif path.endswith((".tar.gz", ".tgz", ".tar")):
                        with tarfile.open(download_path, "r:*") as tar_ref:
                            _safe_extract_tar(tar_ref, repo_dir)
                    else:
                        return "Unsupported URL format. Provide a .git URL or a .zip/.tar archive URL."
                except Exception as e:
                    return f"Failed to download or extract archive from {source_path_or_url}: {e}"
        else:
            if expected_sha256 is not None:
                return "expected_sha256 is only supported for archive download URLs, not local directories."
            # Local path
            local_source = Path(source_path_or_url).expanduser().resolve()
            if not local_source.exists():
                return f"Local path {local_source} does not exist."
            try:
                if local_source.is_dir():
                    _validate_tree_limits(local_source)
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

        try:
            skill_name = _validate_skill_name(parsed_skill.config.name)
            dest_dir = _resolve_skill_dest(user_skills_dir, skill_name)
        except ValueError as e:
            return str(e)

        # 4. Cleanup .git before comparison/copy
        git_dir = skill_dir / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)

        # 5. Validate and install/update
        try:
            _validate_tree_limits(skill_dir)
            return _install_or_update_skill_dir(skill_dir, dest_dir, skill_name)
        except Exception as e:
            return f"Failed to install skill '{skill_name}': {e}"

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
