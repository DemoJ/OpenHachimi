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


def webui_dist_path() -> Path:
    """前端构建产物目录（与 interface/http.py 的挂载判断保持一致）。"""
    return Path(__file__).resolve().parent.parent / "webui_dist"


def webui_url(host: str, port: int) -> str | None:
    """返回 WebUI 访问地址；前端未构建（webui_dist 不存在）时返回 None。"""
    if webui_dist_path().exists():
        return f"http://{host}:{port}/ui/"
    return None


def print_endpoints(host: str, port: int, token: str | None = None) -> None:
    """打印 API、WebUI 访问地址与访问令牌。前端未构建时给出构建提示。"""
    print(f"  API   地址：http://{host}:{port}")
    url = webui_url(host, port)
    if url:
        print(f"  WebUI 地址：{url}")
    else:
        print("  WebUI 地址：（前端未构建，运行 `cd webui && npm run build` 后重启服务）")
    if token:
        print(f"  访问令牌（HTTP API Token）：{token}")
    else:
        print("  访问令牌（HTTP API Token）：（未配置，请检查配置文件 app.http_api_token）")


def deploy_daemon() -> None:
    """注册后台守护服务。

    service 文件直接运行 `hachimi serve`（不写死 host/port），serve 每次启动时
    读取配置文件 app.server_host/server_port。因此改配置后 `hachimi restart` 即生效，
    无需重新 deploy。命令行 --host/--port 由 cmd_deploy 写回配置文件后再生效。
    """
    system_name = platform.system().lower()
    if system_name == "linux" and _command_exists("systemctl"):
        deploy_systemd_user_service()
        return

    deploy_local_script()


def deploy_systemd_user_service() -> None:
    config = load_config()
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / f"{SERVICE_NAME}.service"

    # ExecStart 直接运行 `hachimi serve`，host/port 由 serve 启动时读配置文件决定，
    # 改配置后 restart 即生效，无需重新 deploy。
    service_path.write_text(
        "[Unit]\n"
        "Description=OpenHachimi Agent\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={config.base_dir}\n"
        f"ExecStart={_python_executable()} -m openhachimi_agent serve\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], check=True)

    print(f"已部署并启动 systemd user service：{service_path}")
    print("服务访问地址（host/port 取自配置文件 app.server_host/server_port）：")
    print_endpoints(config.server_host, config.server_port, config.http_api_token)
    print("以后直接运行 hachimi 即可进入 CLI。")


def deploy_local_script() -> None:
    config = load_config()
    script_name = "openhachimi-serve.bat" if platform.system().lower() == "windows" else "openhachimi-serve.sh"
    script_path = config.base_dir / script_name

    # 脚本直接运行 `hachimi serve`，host/port 由 serve 启动时读配置文件决定。
    if script_path.suffix == ".bat":
        content = (
            "@echo off\r\n"
            f"cd /d {config.base_dir}\r\n"
            f"\"{_python_executable()}\" -m openhachimi_agent serve\r\n"
        )
    else:
        content = (
            "#!/usr/bin/env sh\n"
            f"cd '{config.base_dir}'\n"
            f"'{_python_executable()}' -m openhachimi_agent serve\n"
        )

    script_path.write_text(content, encoding="utf-8")
    if script_path.suffix == ".sh":
        script_path.chmod(script_path.stat().st_mode | 0o111)

    print(f"当前系统未检测到可用 systemd，已生成本地启动脚本：{script_path}")
    print("运行该脚本即可启动后台服务（host/port 取自配置文件）：")
    print_endpoints(config.server_host, config.server_port, config.http_api_token)
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
