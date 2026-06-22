"""Shared installation helpers used by deploy and update flows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def run(command: list[str], *, cwd: Path | None = None) -> None:
    """Run a command while echoing it for the user."""
    print("$ " + " ".join(str(item) for item in command))
    subprocess.run(command, cwd=cwd, check=True)


def _run_shell(command: list[str], *, cwd: Path) -> None:
    """运行命令，Windows 下通过 cmd /c 以支持 .cmd/.bat（如 npm）。

    CreateProcess 无法直接执行 .cmd/.bat，必须经由 cmd.exe；POSIX 上 npm 是
    真二进制，直接执行即可。
    """
    print("$ " + " ".join(str(item) for item in command))
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", *command], cwd=cwd, check=True)
    else:
        subprocess.run(command, cwd=cwd, check=True)


def build_webui(project_root: Path) -> None:
    """构建 WebUI 前端到 openhachimi_agent/webui_dist/。

    webui_dist 被 .gitignore 排除，git clone/pull 后不存在，必须构建后 /ui 才可用。
    - 设置环境变量 OPENHACHIMI_SKIP_WEBUI=1 可跳过。
    - Node/npm 缺失或版本过低时打印告警并返回（不阻断后端部署）。
    - 依赖安装/构建失败时抛错（调用方决定是否致命）。
    """
    if os.environ.get("OPENHACHIMI_SKIP_WEBUI"):
        print("[INFO] 已跳过 WebUI 前端构建（OPENHACHIMI_SKIP_WEBUI 已设置）。")
        return

    webui_dir = project_root / "webui"
    if not webui_dir.is_dir():
        print(f"[WARN] 未找到 webui 目录（{webui_dir}），跳过前端构建。")
        return

    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        print("[WARN] 未检测到 Node.js / npm，跳过 WebUI 前端构建。/ui 网页将不可用（API 不受影响）。")
        print("       安装 Node.js 18+ 后重新部署即可：")
        print("         Ubuntu/Debian：curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs")
        print("         macOS：        brew install node")
        return

    # Vite 5 需要 Node 18+
    version_out = subprocess.run(
        [node, "-p", "process.versions.node"],
        capture_output=True, text=True, check=False,
    )
    node_ver = version_out.stdout.strip()
    try:
        node_major = int(node_ver.split(".")[0])
    except (ValueError, IndexError):
        node_major = 0
    if node_major < 18:
        print(f"[WARN] Node.js 版本过低（v{node_ver}），Vite 5 需要 18+，跳过前端构建。")
        return
    print(f"[OK] 检测到 Node.js v{node_ver}")

    print("[INFO] 安装前端依赖...")
    if (webui_dir / "package-lock.json").exists():
        try:
            _run_shell(["npm", "ci", "--no-audit", "--no-fund"], cwd=webui_dir)
        except subprocess.CalledProcessError:
            print("[WARN] npm ci 失败，改用 npm install...")
            _run_shell(["npm", "install", "--no-audit", "--no-fund"], cwd=webui_dir)
    else:
        _run_shell(["npm", "install", "--no-audit", "--no-fund"], cwd=webui_dir)

    print("[INFO] 构建前端（npm run build）...")
    _run_shell(["npm", "run", "build"], cwd=webui_dir)
    print("[OK] 前端构建完成，产物位于 openhachimi_agent/webui_dist/。")


def install_project(project_root: Path, python_executable: str | None = None) -> None:
    """Install the project in editable mode using the active Python by default."""
    python_path = python_executable or sys.executable
    print("[INFO] 安装项目依赖（pip install -e .）...")
    run([python_path, "-m", "pip", "install", "-U", "pip", "--quiet"], cwd=project_root)
    run([python_path, "-m", "pip", "install", "-e", ".", "--quiet"], cwd=project_root)
    print("[OK] 依赖安装完成。")
    build_webui(project_root)
