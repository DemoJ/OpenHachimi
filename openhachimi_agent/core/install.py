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


def _run_shell(command: list[str], *, cwd: Path, timeout: float | None = None) -> None:
    """运行命令，Windows 下通过 cmd /c 以支持 .cmd/.bat（如 npm）。

    CreateProcess 无法直接执行 .cmd/.bat，必须经由 cmd.exe；POSIX 上 npm 是
    真二进制，直接执行即可。timeout 超时会抛 SystemExit 并给出网络排错提示。
    """
    print("$ " + " ".join(str(item) for item in command))
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", *command], cwd=cwd, check=True, timeout=timeout)
        else:
            subprocess.run(command, cwd=cwd, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"[x] 命令超时（{timeout}秒）：{' '.join(command)}\n"
            "    通常是网络问题。可尝试：\n"
            "      1. 配置镜像源：设置环境变量 NPM_CONFIG_REGISTRY=https://registry.npmmirror.com/\n"
            "      2. 跳过前端构建：设置环境变量 OPENHACHIMI_SKIP_WEBUI=1 后重试\n"
            "      3. 手动在 webui/ 目录执行 npm install 排查"
        ) from exc


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
    # npm ci 会删除 node_modules 全量重装，更新场景下太慢且费流量。
    # 策略：node_modules 已存在时走 npm install（增量、--prefer-offline 优先用本地缓存），
    #       仅首次（无 node_modules）才用 npm ci 干净安装。
    # --foreground-scripts 让 esbuild 等 postinstall 输出实时可见，避免「卡住」错觉。
    NPM_INSTALL_FLAGS = ["--no-audit", "--no-fund", "--foreground-scripts"]
    has_lock = (webui_dir / "package-lock.json").exists()
    has_modules = (webui_dir / "node_modules").is_dir()

    if has_modules:
        install_cmd = ["npm", "install", "--prefer-offline", *NPM_INSTALL_FLAGS]
    elif has_lock:
        install_cmd = ["npm", "ci", *NPM_INSTALL_FLAGS]
    else:
        install_cmd = ["npm", "install", *NPM_INSTALL_FLAGS]

    try:
        _run_shell(install_cmd, cwd=webui_dir, timeout=600)
    except subprocess.CalledProcessError:
        # 常见原因：上次安装被中断导致 npm 缓存残留损坏（如 esbuild 平台二进制
        # 包「cache hit 但 no local data」）。清缓存后重装一次以自愈。
        print("[WARN] 前端依赖安装失败，清理 npm 缓存后重试...")
        _run_shell(["npm", "cache", "clean", "--force"], cwd=webui_dir, timeout=120)
        if install_cmd[1] == "ci":
            print("[WARN] 改用 npm install 重试...")
            _run_shell(["npm", "install", *NPM_INSTALL_FLAGS], cwd=webui_dir, timeout=600)
        else:
            _run_shell(install_cmd, cwd=webui_dir, timeout=600)

    print("[INFO] 构建前端（npm run build）...")
    _run_shell(["npm", "run", "build"], cwd=webui_dir, timeout=600)
    print("[OK] 前端构建完成，产物位于 openhachimi_agent/webui_dist/。")


# 外网访问不稳定时，pip 直连 pypi.org 易在 SSL 握手阶段失败（SSLEOFError）。
# 用国内镜像 + trusted-host 兜底，并加大超时与重试次数容忍抖动。
_DEFAULT_PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
_DEFAULT_PIP_TRUSTED_HOST = "pypi.tuna.tsinghua.edu.cn"


def _pip_mirror_args() -> list[str]:
    """返回镜像源参数；用户已通过 PIP_INDEX_URL/PIP_INDEX 显式指定时不覆盖。"""
    if os.environ.get("PIP_INDEX_URL") or os.environ.get("PIP_INDEX"):
        # 用户自行配置了镜像源，交由 pip 处理 trusted-host，这里不干预。
        return []
    return ["-i", _DEFAULT_PIP_INDEX_URL, "--trusted-host", _DEFAULT_PIP_TRUSTED_HOST]


def _print_pip_failure_hint(returncode: int) -> None:
    """依赖安装失败时打印可操作的排错建议。"""
    print(f"[x] 依赖安装失败，pip 退出码：{returncode}")
    print("  常见原因是外网访问不稳定（SSL 握手失败）。可尝试：")
    print("    1. 指定镜像源：设置环境变量 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple")
    print("    2. 走代理：设置环境变量 HTTPS_PROXY=http://127.0.0.1:7890")
    print("    3. 修复后重装：hachimi update --force")


def install_project(project_root: Path, python_executable: str | None = None) -> None:
    """Install the project in editable mode using the active Python by default.

    pip 升级非必需，失败时只告警不中断；项目依赖安装失败才中止并给出镜像源/代理建议。
    镜像源参数会传递给 PEP 517 build isolation 阶段，build 依赖（setuptools/wheel）
    的拉取同样走镜像，避免 build dependencies 阶段 SSL 失败。
    """
    python_path = python_executable or sys.executable
    mirror_args = _pip_mirror_args()
    robust_args = ["--retries", "5", "--timeout", "60"]

    print("[INFO] 升级 pip ...")
    pip_upgrade_cmd = [python_path, "-m", "pip", "install", "-U", "pip",
                       *robust_args, *mirror_args, "--quiet"]
    try:
        run(pip_upgrade_cmd, cwd=project_root)
    except subprocess.CalledProcessError:
        print("[WARN] pip 升级失败，已跳过（不影响后续依赖安装）。")

    print("[INFO] 安装项目依赖（pip install -e .）...")
    install_cmd = [python_path, "-m", "pip", "install", "-e", ".",
                   *robust_args, *mirror_args, "--quiet"]
    try:
        run(install_cmd, cwd=project_root)
    except subprocess.CalledProcessError as exc:
        _print_pip_failure_hint(exc.returncode)
        raise

    print("[OK] 依赖安装完成。")
    build_webui(project_root)
