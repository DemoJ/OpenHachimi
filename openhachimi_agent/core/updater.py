"""Git-based automatic updater.

Updates are driven by commit hashes instead of the package version, because
code can change without a version bump.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from openhachimi_agent.core.install import install_project
from openhachimi_agent.core.version import get_version


_VERSION_PATTERN = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


@dataclass(frozen=True)
class GitRef:
    remote: str
    branch: str

    @property
    def name(self) -> str:
        return f"{self.remote}/{self.branch}"


def _run_git(project_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={project_root.as_posix()}", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _find_git_root_marker(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists() and (candidate / "pyproject.toml").exists():
            return candidate
    return None


def _git_root_from(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={path.as_posix()}", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _find_git_root_marker(path)


def _installed_project_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _get_project_root() -> Path | None:
    """Locate the OpenHachimi git root from cwd or the installed package path."""
    cwd_root = _git_root_from(Path.cwd())
    if cwd_root is not None:
        return cwd_root

    package_root = _git_root_from(_installed_project_dir())
    if package_root is not None:
        return package_root

    return None


def _git_output(project_root: Path, *args: str) -> str:
    return _run_git(project_root, *args).stdout.strip()


def _short_commit(project_root: Path, ref: str) -> str:
    return _git_output(project_root, "rev-parse", "--short", ref)


def _full_commit(project_root: Path, ref: str) -> str:
    return _git_output(project_root, "rev-parse", ref)


def _is_working_tree_dirty(project_root: Path) -> bool:
    return bool(_git_output(project_root, "status", "--porcelain"))


def _current_branch(project_root: Path) -> str | None:
    try:
        branch = _git_output(project_root, "symbolic-ref", "--short", "HEAD")
    except subprocess.CalledProcessError:
        return None
    return branch or None


def _upstream_ref(project_root: Path) -> GitRef | None:
    try:
        upstream = _git_output(project_root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    except subprocess.CalledProcessError:
        return None
    if "/" not in upstream:
        return None
    remote, branch = upstream.split("/", 1)
    return GitRef(remote=remote, branch=branch)


def _remote_head_branch(project_root: Path, remote: str) -> str | None:
    try:
        remote_head = _git_output(project_root, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD")
    except subprocess.CalledProcessError:
        return None
    prefix = f"{remote}/"
    if remote_head.startswith(prefix):
        return remote_head[len(prefix):]
    return None


def _default_remote_ref(project_root: Path) -> GitRef | None:
    remotes = _git_output(project_root, "remote").splitlines()
    if not remotes:
        return None

    remote = "origin" if "origin" in remotes else remotes[0]
    branch = _remote_head_branch(project_root, remote) or _current_branch(project_root) or "main"
    if branch in {"HEAD", ""}:
        branch = "main"
    return GitRef(remote=remote, branch=branch)


def _resolve_update_ref(project_root: Path) -> GitRef | None:
    return _upstream_ref(project_root) or _default_remote_ref(project_root)


def _fetch(project_root: Path, ref: GitRef) -> bool:
    print(f"正在拉取远端信息：{ref.remote} {ref.branch} ...")
    remote_tracking_ref = f"refs/remotes/{ref.remote}/{ref.branch}"
    result = _run_git(project_root, "fetch", ref.remote, f"{ref.branch}:{remote_tracking_ref}", check=False)
    if result.returncode == 0:
        return True

    print("[x] git fetch 失败：")
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    return False


def _is_ancestor(project_root: Path, older_ref: str, newer_ref: str) -> bool:
    result = _run_git(project_root, "merge-base", "--is-ancestor", older_ref, newer_ref, check=False)
    return result.returncode == 0


def _version_from_ref(project_root: Path, ref: str) -> str | None:
    try:
        text = _git_output(project_root, "show", f"{ref}:pyproject.toml")
    except subprocess.CalledProcessError:
        return None
    match = _VERSION_PATTERN.search(text)
    return match.group(1) if match else None


def _merge_ff_only(project_root: Path, ref: str) -> bool:
    result = _run_git(project_root, "merge", "--ff-only", ref, check=False)
    if result.returncode == 0:
        if result.stdout.strip():
            print(result.stdout.strip())
        return True

    print("[x] git merge --ff-only 失败：")
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    return False


def _print_restart_hint() -> None:
    print("  如果正在运行后台守护服务，请重启以使更新生效：")
    print("    systemctl --user restart openhachimi   # Linux systemd")
    print("    或重新运行 hachimi deploy")


def run_update(*, force: bool = False) -> None:
    """Fetch remote code, update when the remote commit changed, then reinstall."""
    local_version = get_version()
    print(f"当前安装版本：{local_version}")

    project_root = _get_project_root()
    if project_root is None:
        print("[x] 未检测到 Git 仓库，无法自动更新。")
        print(f"  已尝试当前目录和安装目录：{_installed_project_dir()}")
        print("  如果这是非 Git 方式安装的副本，请手动执行安装脚本重新部署。")
        return
    print(f"项目目录：{project_root}")

    update_ref = _resolve_update_ref(project_root)
    if update_ref is None:
        print("[x] 未检测到 Git remote，无法自动更新。")
        print("  请先为仓库配置远端，例如：git remote add origin <repo-url>")
        return

    if not _fetch(project_root, update_ref):
        return

    local_commit = _full_commit(project_root, "HEAD")
    remote_commit = _full_commit(project_root, update_ref.name)
    local_short = _short_commit(project_root, "HEAD")
    remote_short = _short_commit(project_root, update_ref.name)
    remote_version = _version_from_ref(project_root, update_ref.name) or "未知"

    print(f"本地 commit：{local_short}")
    print(f"远端 commit：{remote_short} ({update_ref.name})")
    print(f"远端版本号：{remote_version}")

    if local_commit == remote_commit:
        if not force:
            print("[ok] 当前代码已是最新 commit，无需更新。")
            print("     如需仅重新安装依赖，可运行：hachimi update --force")
            return

        print("[INFO] 本地 commit 与远端一致，按 --force 重新安装依赖。")
        try:
            install_project(project_root)
        except subprocess.CalledProcessError as exc:
            print(f"[x] 重新安装失败，命令退出码：{exc.returncode}")
            return
        print("\n[ok] 重新安装完成。")
        _print_restart_hint()
        return

    if _is_working_tree_dirty(project_root):
        print("[x] 检测到本地有未提交的修改，为防止覆盖已中止更新。")
        print("  请先提交或暂存你的修改：")
        print("    git stash    # 暂存修改")
        print("    git commit   # 或提交修改")
        print("  然后再次运行 hachimi update。")
        return

    if _is_ancestor(project_root, "HEAD", update_ref.name):
        if remote_version == local_version:
            print("[INFO] 远端代码有更新，但版本号未变化；仍将按 commit 更新。")
        print("\n正在快进到远端最新代码...")
        if not _merge_ff_only(project_root, update_ref.name):
            return
    elif _is_ancestor(project_root, update_ref.name, "HEAD"):
        print("[ok] 本地 commit 比远端更新，无需更新。")
        return
    else:
        print("[x] 本地分支和远端分支已分叉，无法安全自动更新。")
        print(f"  本地：HEAD {local_short}")
        print(f"  远端：{update_ref.name} {remote_short}")
        print("  请手动处理 merge/rebase 后再运行 hachimi update。")
        return

    new_local_short = _short_commit(project_root, "HEAD")
    print("\n正在重新安装依赖...")
    try:
        install_project(project_root)
    except subprocess.CalledProcessError as exc:
        print(f"[x] 重新安装失败，命令退出码：{exc.returncode}")
        print("  代码已经更新，请修复安装问题后运行：hachimi update --force")
        return

    print(f"\n[ok] 更新完成：{local_short} -> {new_local_short}")
    print(f"     版本号：{local_version} -> {remote_version}")
    _print_restart_hint()


def main() -> None:
    run_update(force="--force" in sys.argv[1:])
