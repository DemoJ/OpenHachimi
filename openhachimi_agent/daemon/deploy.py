"""后台守护部署逻辑。"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from openhachimi_agent.core.config import load_config


SERVICE_NAME = "openhachimi"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _python_executable() -> str:
    return sys.executable


def _command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def deploy_daemon(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    system_name = platform.system().lower()
    if system_name == "linux" and _command_exists("systemctl"):
        deploy_systemd_user_service(host, port)
        return

    deploy_local_script(host, port)


def deploy_systemd_user_service(host: str, port: int) -> None:
    config = load_config()
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / f"{SERVICE_NAME}.service"

    service_path.write_text(
        "[Unit]\n"
        "Description=OpenHachimi Agent\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={config.base_dir}\n"
        f"ExecStart={_python_executable()} -m openhachimi_agent serve --host {host} --port {port}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], check=True)

    print(f"已部署并启动 systemd user service：{service_path}")
    print(f"服务地址：http://{host}:{port}")
    print("以后直接运行 hachimi 即可进入 CLI。")


def deploy_local_script(host: str, port: int) -> None:
    config = load_config()
    script_name = "openhachimi-serve.bat" if platform.system().lower() == "windows" else "openhachimi-serve.sh"
    script_path = config.base_dir / script_name

    if script_path.suffix == ".bat":
        content = (
            "@echo off\r\n"
            f"cd /d {config.base_dir}\r\n"
            f"\"{_python_executable()}\" -m openhachimi_agent serve --host {host} --port {port}\r\n"
        )
    else:
        content = (
            "#!/usr/bin/env sh\n"
            f"cd '{config.base_dir}'\n"
            f"'{_python_executable()}' -m openhachimi_agent serve --host {host} --port {port}\n"
        )

    script_path.write_text(content, encoding="utf-8")
    if script_path.suffix == ".sh":
        script_path.chmod(script_path.stat().st_mode | 0o111)

    print(f"当前系统未检测到可用 systemd，已生成本地启动脚本：{script_path}")
    print(f"运行该脚本即可启动后台服务：http://{host}:{port}")
    print("服务启动后，直接运行 hachimi 即可进入 CLI。")


def undeploy_daemon(remove_venv: bool = False, remove_project: bool = False) -> None:
    """卸载后台守护服务。

    参数：
        remove_venv:    是否同时删除虚拟环境（.venv 目录）。
        remove_project: 是否同时删除整个项目目录（需同时开启 remove_venv）。
    """
    import shutil

    system_name = platform.system().lower()

    # ── 1. 停止并注销 systemd 服务 ─────────────────────────────────────────
    if system_name == "linux" and _command_exists("systemctl"):
        service_file = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"

        # 先尝试停止，忽略"服务未运行"的错误
        subprocess.run(["systemctl", "--user", "stop",    SERVICE_NAME], check=False)
        subprocess.run(["systemctl", "--user", "disable", SERVICE_NAME], check=False)

        if service_file.exists():
            service_file.unlink()
            print(f"已删除 systemd service 文件：{service_file}")
        else:
            print(f"未找到 service 文件（可能已删除）：{service_file}")

        subprocess.run(["systemctl", "--user", "daemon-reload"],  check=False)
        subprocess.run(["systemctl", "--user", "reset-failed"],   check=False)
        print("systemd 服务已停止并注销。")
    else:
        # 非 systemd 环境：尝试删除本地启动脚本
        try:
            config = load_config()
            for ext in (".sh", ".bat"):
                script = config.base_dir / f"openhachimi-serve{ext}"
                if script.exists():
                    script.unlink()
                    print(f"已删除本地启动脚本：{script}")
        except Exception:
            pass  # 配置文件可能已不存在，忽略

    # ── 2. 可选：删除虚拟环境 ─────────────────────────────────────────────
    if remove_venv:
        # 定位 .venv 目录（相对于 __file__ 向上两级即项目根）
        venv_dir = Path(__file__).resolve().parents[2] / ".venv"
        if venv_dir.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)
            print(f"已删除虚拟环境：{venv_dir}")
        else:
            print("未找到虚拟环境目录，跳过。")

    # ── 3. 可选：删除整个项目目录 ─────────────────────────────────────────
    if remove_project:
        project_dir = Path(__file__).resolve().parents[2]
        print(f"正在删除项目目录：{project_dir}")
        shutil.rmtree(project_dir, ignore_errors=True)
        print("项目目录已删除。")
        # 删除自身后无法继续执行，直接退出
        sys.exit(0)
