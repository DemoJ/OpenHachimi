"""Shared installation helpers used by deploy and update flows."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run(command: list[str], *, cwd: Path | None = None) -> None:
    """Run a command while echoing it for the user."""
    print("$ " + " ".join(str(item) for item in command))
    subprocess.run(command, cwd=cwd, check=True)


def install_project(project_root: Path, python_executable: str | None = None) -> None:
    """Install the project in editable mode using the active Python by default."""
    python_path = python_executable or sys.executable
    print("[INFO] 安装项目依赖（pip install -e .）...")
    run([python_path, "-m", "pip", "install", "-U", "pip", "--quiet"], cwd=project_root)
    run([python_path, "-m", "pip", "install", "-e", ".", "--quiet"], cwd=project_root)
    print("[OK] 依赖安装完成。")
