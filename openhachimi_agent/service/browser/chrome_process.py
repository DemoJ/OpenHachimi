"""Chrome 进程相关工具：可执行文件查找（含降级）、启动参数、单例锁清理、
stderr 尾部读取、进程组终止。

原逻辑内嵌在 lifecycle.py,此处独立成模块函数,保持纯同步（进程操作由
调用方用 asyncio.to_thread 包裹,避免阻塞事件循环）。
"""

import glob
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from openhachimi_agent.core.config import AppConfig

logger = logging.getLogger(__name__)

_CHANNEL_ALIASES = {
    "chrome": ["google-chrome", "google-chrome-stable"],
    "google-chrome": ["google-chrome", "google-chrome-stable"],
    "chromium": ["chromium-browser", "chromium"],
    "msedge": ["microsoft-edge", "microsoft-edge-stable"],
    "edge": ["microsoft-edge", "microsoft-edge-stable"],
}

_WINDOWS_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

_MACOS_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

_LINUX_COMMANDS = [
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
    "microsoft-edge",
    "microsoft-edge-stable",
]


def find_chrome_executable(config: AppConfig) -> str:
    """寻找系统中真实的 Chrome/Edge 可执行文件路径；找不到时降级 playwright chromium。"""
    config_path = getattr(config, "browser_channel", "") or ""
    if config_path and os.path.isabs(config_path) and os.path.exists(config_path):
        return config_path
    config_alias = config_path.lower() if config_path else ""

    if config_alias in _CHANNEL_ALIASES:
        for command in _CHANNEL_ALIASES[config_alias]:
            cmd = shutil.which(command)
            if cmd:
                return cmd

    if sys.platform == "win32":
        for p in _WINDOWS_PATHS:
            if os.path.exists(p):
                return p
    elif sys.platform == "darwin":
        for p in _MACOS_PATHS:
            if os.path.exists(p):
                return p
    else:
        for p in _LINUX_COMMANDS:
            cmd = shutil.which(p)
            if cmd:
                return cmd

    fallback = find_playwright_chromium()
    if fallback:
        logger.warning("未找到系统 Chrome/Edge，降级使用 Playwright 自带 Chromium: %s", fallback)
        return fallback

    raise RuntimeError(
        "无法找到系统中安装的 Chrome 或 Edge 浏览器（Playwright 自带 Chromium 也未安装）。"
        "请先执行 `hachimi install` 安装浏览器驱动，或修改 app.browser_channel 指定 chrome、msedge 或绝对路径。"
    )


def find_playwright_chromium() -> str | None:
    """降级兜底：在 ms-playwright 缓存目录中查找 chromium 可执行文件。"""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
        patterns = [str(root / "chromium-*" / "chrome-win" / "chrome.exe")]
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "ms-playwright"
        patterns = [
            str(root / "chromium-*" / "chrome-mac" / "Chromium"),
            str(root / "chromium-*" / "chrome-mac" / "chrome"),
        ]
    else:
        root = Path.home() / ".cache" / "ms-playwright"
        patterns = [str(root / "chromium-*" / "chrome-linux" / "chrome")]

    for pattern in patterns:
        for match in sorted(glob.glob(pattern)):
            if os.path.isfile(match):
                return match
    return None


def build_launch_args(
    config: AppConfig,
    chrome_path: str,
    port: int,
    user_data_dir: Path,
    window_size: str,
    headless: bool,
) -> list[str]:
    """构建 Chrome 启动参数（CDP 接管模式）。"""
    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--password-store=basic",
        f"--window-size={window_size}",
    ]

    if config.browser_user_agent:
        args.append(f"--user-agent={config.browser_user_agent}")

    if sys.platform == "linux":
        args.extend([
            "--no-sandbox",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ])
        # Wayland 优先：不传 --ozone-platform，由 Chrome 自动选择——
        # WAYLAND_DISPLAY 存活时原生 Wayland，否则自动落回 X11。
        # （历史问题：锁屏/闲置时 mutter 不处理新窗口握手，X11/Wayland 都会卡死，
        # 已由 browser_process_env 的锁屏检测提前拦截并引导用户解锁。）
    if headless:
        args.extend(["--headless=new"])
    return args


def read_devtools_active_port(user_data_dir) -> tuple[int, str] | None:
    """读取 Chrome 启动后写入 user-data-dir 的 DevToolsActivePort 文件。

    第一行是实际监听端口（即便 --remote-debugging-port 因冲突回落到随机端口，
    这里也是真实端口），第二行是 ws 路径（如 /devtools/browser/xxxx）。
    当 Chrome 还没写完该文件时返回 None。
    """
    try:
        path = user_data_dir / "DevToolsActivePort"
    except TypeError:
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not content:
        return None
    try:
        actual_port = int(content[0].strip())
    except (ValueError, IndexError):
        return None
    ws_path = content[1].strip() if len(content) > 1 else ""
    return actual_port, ws_path


def cleanup_stale_singletons(user_data_dir: Path) -> None:
    """启动新 Chrome 前清理可能残留的单例锁。

    当上一次 Chrome 进程异常退出（崩溃、kill -9、宿主服务重启）时，
    user-data-dir 下的 SingletonLock/SingletonSocket/SingletonCookie（Linux）
    或 lockfile（部分 Windows 版本）可能残留，导致新进程检测到"已有实例"后
    把命令行转发给一个不存在的进程然后悄悄退出。
    """
    names = ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile")
    for name in names:
        target = user_data_dir / name
        try:
            if target.is_symlink() or target.exists():
                target.unlink()
                logger.info("已清理残留单例锁: %s", target)
        except OSError as exc:
            logger.warning("清理残留单例锁 %s 失败: %s", target, exc)


def tail_chrome_stderr(log_dir: Path, max_bytes: int = 8192, max_lines: int = 40) -> str:
    """读取 chrome-browser.log 尾部内容（排障用）。"""
    stderr_path = log_dir / "chrome-browser.log"
    if not stderr_path.exists():
        return ""
    try:
        with stderr_path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            file_size = file.tell()
            file.seek(max(file_size - max_bytes, 0))
            stderr_bytes = file.read()
    except Exception:
        return ""
    if not stderr_bytes:
        return ""
    text = stderr_bytes.decode("utf-8", errors="replace").strip()
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def terminate_browser_process(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """终止 Chrome 进程：POSIX 杀整个进程组，Windows terminate→wait→kill。

    同步函数，调用方用 asyncio.to_thread 包裹，避免阻塞事件循环。
    """
    if sys.platform == "win32":
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, Exception):
            try:
                proc.kill()
            except Exception:
                pass
        return

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass