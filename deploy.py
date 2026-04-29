#!/usr/bin/env python3
"""OpenHachimi 一键部署脚本。"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("$ " + " ".join(command))
    subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True)


def find_python() -> str:
    candidates = [sys.executable, shutil.which("python3"), shutil.which("python")]
    for candidate in candidates:
        if candidate:
            return candidate
    raise SystemExit("未找到 Python，请先安装 Python 3.10 或更高版本。")


def venv_python() -> Path:
    if platform.system().lower() == "windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def venv_hachimi() -> Path:
    if platform.system().lower() == "windows":
        return VENV_DIR / "Scripts" / "hachimi.exe"
    return VENV_DIR / "bin" / "hachimi"


def ensure_venv() -> None:
    if venv_python().exists():
        return
    run([find_python(), "-m", "venv", str(VENV_DIR)])


def install_project() -> None:
    python_path = str(venv_python())
    run([python_path, "-m", "pip", "install", "-U", "pip"])
    run([python_path, "-m", "pip", "install", "-e", "."])


def write_config_if_missing() -> None:
    config_path = PROJECT_ROOT / "user" / "config.yaml"
    example_path = PROJECT_ROOT / "user" / "config.example.yaml"
    if config_path.exists() or not example_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example_path, config_path)
    print(f"已创建 {config_path}，请确认其中的 llm.api_key 等配置。")


def deploy_daemon(host: str, port: int) -> None:
    run([str(venv_hachimi()), "deploy", "--host", host, "--port", str(port)])


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenHachimi 一键部署脚本")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--skip-daemon", action="store_true", help="只安装，不部署后台守护")
    args = parser.parse_args()

    ensure_venv()
    install_project()
    write_config_if_missing()

    if not args.skip_daemon:
        deploy_daemon(args.host, args.port)

    print("部署完成。")
    print(f"CLI 命令：{venv_hachimi()}")
    print("如果已把虚拟环境 Scripts/bin 目录加入 PATH，也可以直接运行 hachimi。")


if __name__ == "__main__":
    main()
