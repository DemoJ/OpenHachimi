"""后台守护部署逻辑。"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from openhachimi_agent.config import load_config


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
    env_file = config.base_dir / ".env"
    environment_file_line = f"EnvironmentFile={env_file}\n" if env_file.exists() else ""

    service_path.write_text(
        "[Unit]\n"
        "Description=OpenHachimi Agent\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={config.base_dir}\n"
        f"{environment_file_line}"
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
