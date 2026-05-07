#!/usr/bin/env python3
"""
OpenHachimi 一键部署脚本（跨平台，支持自举）

用法一：直接下载运行（自动 clone 项目，适用于 Windows/Linux/macOS）
  python deploy.py

用法二：在项目目录中运行
  python deploy.py [选项]

选项：
  --host HOST       后台服务监听地址（默认 127.0.0.1）
  --port PORT       后台服务监听端口（默认 8765）
  --skip-daemon     只安装依赖，不部署后台守护服务
  --repo URL        自定义 Git 仓库地址
  --dir DIR         指定克隆目标目录（默认 ./OpenHachimi）
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


REPO_URL = "https://github.com/DemoJ/OpenHachimi.git"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("$ " + " ".join(str(c) for c in command))
    subprocess.run(command, cwd=cwd, check=True)


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def find_python() -> str:
    candidates = [sys.executable, shutil.which("python3"), shutil.which("python")]
    for candidate in candidates:
        if candidate:
            return candidate
    raise SystemExit("未找到 Python，请先安装 Python 3.10 或更高版本。")


def check_python_version() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit(
            f"当前 Python 版本为 {sys.version_info.major}.{sys.version_info.minor}，"
            "需要 3.10 或更高版本。"
        )


def ensure_project(repo_url: str, clone_dir: Path) -> Path:
    """
    确保项目目录存在。
    - 如果当前目录有 pyproject.toml，直接使用当前目录。
    - 否则自动 git clone 项目到指定目录。
    返回项目根目录路径。
    """
    current = Path.cwd()
    if (current / "pyproject.toml").exists():
        print(f"[OK] 已在项目目录中：{current}")
        return current

    # 自举：clone 项目
    if not shutil.which("git"):
        raise SystemExit("未找到 git，请先安装 git 再重试。")

    if (clone_dir / ".git").exists():
        print(f"[INFO] 目录已存在，拉取最新代码：{clone_dir}")
        run(["git", "-C", str(clone_dir), "pull", "--ff-only"])
    else:
        print(f"[INFO] 克隆项目到：{clone_dir}")
        run(["git", "clone", repo_url, str(clone_dir)])

    print(f"[OK] 项目已就绪：{clone_dir}")
    return clone_dir


def venv_python(project_root: Path) -> Path:
    venv_dir = project_root / ".venv"
    if is_windows():
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_hachimi(project_root: Path) -> Path:
    venv_dir = project_root / ".venv"
    if is_windows():
        return venv_dir / "Scripts" / "hachimi.exe"
    return venv_dir / "bin" / "hachimi"


def ensure_venv(project_root: Path) -> None:
    python_path = venv_python(project_root)
    if python_path.exists():
        print(f"[OK] 虚拟环境已存在，复用：{python_path.parent.parent}")
        return

    print(f"[INFO] 创建虚拟环境：{project_root / '.venv'}")
    venv_cmd = [find_python(), "-m", "venv", str(project_root / ".venv")]

    result = subprocess.run(venv_cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("[OK] 虚拟环境创建完成。")
        return

    # 创建失败，检测是否为 Debian/Ubuntu 缺少 python3-venv 的问题
    combined = (result.stdout + result.stderr).lower()
    if "ensurepip" in combined or "venv" in combined:
        minor = sys.version_info.minor
        venv_pkg = f"python3.{minor}-venv"
        print(f"[WARN] 检测到缺少 {venv_pkg}，尝试自动安装...")

        apt = shutil.which("apt-get")
        if apt:
            ret = subprocess.run(["sudo", apt, "install", "-y", venv_pkg])
            if ret.returncode == 0:
                print(f"[OK] 已安装 {venv_pkg}，重新创建虚拟环境...")
                run(venv_cmd)
                print("[OK] 虚拟环境创建完成。")
                return
            else:
                raise SystemExit(
                    f"自动安装 {venv_pkg} 失败，请手动执行：\n"
                    f"  sudo apt-get install -y {venv_pkg}\n"
                    "然后重新运行此脚本。"
                )
        else:
            print(result.stderr, file=sys.stderr)
            raise SystemExit(
                "创建虚拟环境失败。请先安装 python3-venv（或等效包）后重试。"
            )

    print(result.stderr, file=sys.stderr)
    raise SystemExit("创建虚拟环境失败，请查看上方错误信息。")



def install_project(project_root: Path) -> None:
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from openhachimi_agent.core.install import install_project as shared_install_project

    shared_install_project(project_root, python_executable=str(venv_python(project_root)))


def write_config_if_missing(project_root: Path) -> None:
    config_path = project_root / "user" / "config.yaml"
    example_path = project_root / "user" / "config.example.yaml"
    if config_path.exists():
        print(f"[OK] 配置文件已存在：{config_path}")
        return
    if not example_path.exists():
        print("[WARN] 未找到配置模板，跳过配置文件初始化。")
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example_path, config_path)
    print(f"[WARN] 已从模板创建配置文件：{config_path}")
    print("[WARN] ⚠  请在启动服务前填写 llm.api_key 等配置！")


def deploy_daemon(project_root: Path, host: str, port: int) -> None:
    hachimi = venv_hachimi(project_root)
    run([str(hachimi), "deploy", "--host", host, "--port", str(port)], cwd=project_root)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenHachimi 一键部署脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="后台服务监听地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="后台服务监听端口")
    parser.add_argument("--skip-daemon", action="store_true", help="只安装，不部署后台守护")
    parser.add_argument("--repo", default=REPO_URL, help="自定义 Git 仓库地址")
    parser.add_argument("--dir", default="./OpenHachimi", help="克隆目标目录")
    args = parser.parse_args()

    print("\n=== OpenHachimi 一键部署 ===\n")

    # 步骤 1：检查 Python 版本
    check_python_version()
    print(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}")

    # 步骤 2：确保项目目录存在（自举 clone）
    project_root = ensure_project(args.repo, Path(args.dir).resolve())

    # 步骤 3：创建虚拟环境
    ensure_venv(project_root)

    # 步骤 4：安装依赖
    install_project(project_root)

    # 步骤 5：初始化配置文件
    write_config_if_missing(project_root)

    # 步骤 6：部署后台守护
    if not args.skip_daemon:
        deploy_daemon(project_root, args.host, args.port)

    print("\n========================================")
    print("  部署完成！")
    print("========================================\n")
    print(f"  项目目录：{project_root}")
    print(f"  可执行文件：{venv_hachimi(project_root)}")
    print("\n  常用命令：")
    hachimi_cmd = str(venv_hachimi(project_root))
    print(f"    进入 CLI 对话：  {hachimi_cmd}")
    if not is_windows():
        venv_bin = project_root / ".venv" / "bin"
        print(f"\n  如需将 hachimi 加入全局 PATH：")
        print(f"    echo 'export PATH=\"{venv_bin}:$PATH\"' >> ~/.bashrc && source ~/.bashrc")

    config_path = project_root / "user" / "config.yaml"
    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        if "sk-xxxxxxxx" in content:
            print(f"\n  [提醒] 配置文件中仍使用示例 API Key，请记得修改：{config_path}")


if __name__ == "__main__":
    main()
