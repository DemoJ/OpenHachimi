"""自动更新模块。

从 GitHub 获取远程版本号，与本地版本对比，有新版本时自动拉取并重新安装。
"""

import re
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from openhachimi_agent.core.version import PACKAGE_NAME, get_version

# GitHub 远程 pyproject.toml 的 Raw URL
REMOTE_PYPROJECT_URL = (
    "https://raw.githubusercontent.com/DemoJ/OpenHachimi/main/pyproject.toml"
)

# 从 pyproject.toml 文本中提取 version 字段的正则
_VERSION_PATTERN = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def _fetch_remote_version() -> str | None:
    """从 GitHub 获取远程 pyproject.toml 中的版本号。"""
    try:
        request = Request(REMOTE_PYPROJECT_URL, headers={"User-Agent": "OpenHachimi-Updater"})
        with urlopen(request, timeout=15) as response:
            text = response.read().decode("utf-8")
    except (URLError, OSError) as exc:
        print(f"[!] 无法连接 GitHub 获取远程版本信息：{exc}")
        return None

    match = _VERSION_PATTERN.search(text)
    if not match:
        print("[!] 远程 pyproject.toml 中未找到版本号。")
        return None

    return match.group(1)


def _get_project_root() -> Path | None:
    """通过 git 获取项目根目录。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _is_working_tree_dirty(project_root: Path) -> bool:
    """检查工作区是否有未提交的修改。"""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        return bool(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _compare_versions(local: str, remote: str) -> int:
    """比较两个语义化版本号。

    返回值：
        -1: local < remote（有新版本）
         0: local == remote（已是最新）
         1: local > remote（本地更新）
    """
    try:
        from packaging.version import Version

        lv, rv = Version(local), Version(remote)
        if lv < rv:
            return -1
        if lv > rv:
            return 1
        return 0
    except Exception:
        # 如果 packaging 不可用，退化为字符串比较
        if local == remote:
            return 0
        return -1


def run_update() -> None:
    """执行更新流程。"""
    local_version = get_version()
    print(f"当前版本：{local_version}")
    print("正在检查远程版本...")

    remote_version = _fetch_remote_version()
    if remote_version is None:
        print("无法获取远程版本信息，更新中止。")
        return

    print(f"远程版本：{remote_version}")

    if local_version != "dev":
        cmp = _compare_versions(local_version, remote_version)
        if cmp == 0:
            print("[ok] 当前已是最新版本，无需更新。")
            return
        if cmp == 1:
            print("[ok] 本地版本比远程更新，无需更新。")
            return

    # 有新版本，开始更新
    project_root = _get_project_root()
    if project_root is None:
        print("[x] 未检测到 Git 仓库，无法自动更新。")
        print("  请手动执行 git pull 并重新安装。")
        return

    # 检查工作区是否干净
    if _is_working_tree_dirty(project_root):
        print("[x] 检测到本地有未提交的修改，为防止覆盖已中止更新。")
        print("  请先提交或暂存你的修改：")
        print("    git stash    # 暂存修改")
        print("    git commit   # 或提交修改")
        print("  然后再次运行 hachimi update。")
        return

    # 执行 git pull
    print("\n正在拉取最新代码...")
    try:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=project_root,
            check=True,
        )
    except subprocess.CalledProcessError:
        print("[x] git pull 失败，请手动处理后重试。")
        return

    # 重新安装
    print("\n正在重新安装依赖...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", "."],
            cwd=project_root,
            check=True,
        )
    except subprocess.CalledProcessError:
        print("[x] pip install 失败，请手动检查。")
        return

    print(f"\n[ok] 更新完成：{local_version} -> {remote_version}")
    print("  如果正在运行后台守护服务，请重启以使更新生效：")
    print("    systemctl --user restart openhachimi   # Linux systemd")
    print("    或重新运行 hachimi deploy")
