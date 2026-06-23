"""Lifecycle management for the Playwright browser."""

import asyncio
import json
import logging
import os
import random
import socket
import sys
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright_stealth import Stealth

from openhachimi_agent.core.config import AppConfig

logger = logging.getLogger(__name__)


class BrowserLifecycleMixin:
    """Mixin providing browser startup, process management, and connection logic.
    
    Expects the class to have the following attributes initialized:
    self.config: AppConfig
    self._playwright
    self._browser: Browser | None
    self._context: BrowserContext | None
    self._page: Page | None
    self._chrome_process
    self._chrome_stderr_file
    self._chrome_cdp_port: int | None  # 记录当前 CDP 端口，用于复用检测
    self._lock: asyncio.Lock
    self._op_lock: asyncio.Lock
    """

    def _ensure_local_proxy_bypass(self) -> None:
        """保留外网代理，但强制本机 CDP 连接绕过代理。"""
        local_hosts = ["127.0.0.1", "localhost", "::1"]
        for key in ("NO_PROXY", "no_proxy"):
            values = [
                item.strip()
                for item in os.environ.get(key, "").split(",")
                if item.strip()
            ]
            existing = {item.lower() for item in values}
            for host in local_hosts:
                if host.lower() not in existing:
                    values.append(host)
            os.environ[key] = ",".join(values)

    def _find_free_port(self) -> int:
        """让系统分配一个当前可用的本地端口，避免固定 9222 端口冲突。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _get_cdp_websocket_url(self, port: int) -> str | None:
        """用原生 socket 读取 CDP websocket 地址，避免 urllib/代理和 /json/version/ 兼容问题。"""
        request = (
            "GET /json/version HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
                sock.settimeout(1)
                sock.sendall(request)
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = sock.recv(4096)
                    except TimeoutError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
        except OSError:
            return None

        response = b"".join(chunks)
        if not (b" 200 " in response or response.startswith(b"HTTP/1.1 200")):
            return None
        _, _, body = response.partition(b"\r\n\r\n")
        if not body:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        websocket_url = payload.get("webSocketDebuggerUrl")
        if not isinstance(websocket_url, str) or not websocket_url.startswith("ws"):
            return None
        return self._normalize_cdp_websocket_url(websocket_url, port)

    def _normalize_cdp_websocket_url(self, websocket_url: str, port: int) -> str:
        """有些代理/Host 场景下 Chrome 返回的 ws 地址缺端口，这里补回实际 CDP 端口。"""
        parsed = urlsplit(websocket_url)
        if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port is None:
            netloc = f"{parsed.hostname}:{port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        return websocket_url

    def _read_process_environ(self, pid: str) -> dict[str, str]:
        environ_path = f"/proc/{pid}/environ"
        try:
            with open(environ_path, "rb") as file:
                raw = file.read()
        except OSError:
            return {}

        env: dict[str, str] = {}
        for item in raw.split(b"\0"):
            if not item or b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            try:
                env[key.decode("utf-8")] = value.decode("utf-8")
            except UnicodeDecodeError:
                continue
        return env

    def _discover_linux_desktop_env(self) -> dict[str, str]:
        """从同用户的图形会话进程中补齐 systemd 后台服务缺失的桌面环境变量。"""
        if sys.platform != "linux":
            return {}

        wanted = {
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "DBUS_SESSION_BUS_ADDRESS",
            "XDG_CURRENT_DESKTOP",
            "XDG_RUNTIME_DIR",
            "XDG_SESSION_TYPE",
        }
        current = {key: value for key in wanted if (value := os.environ.get(key))}
        if current.get("DISPLAY") or current.get("WAYLAND_DISPLAY"):
            return current

        uid = os.getuid()
        preferred_names = (
            "gnome-session",
            "gnome-shell",
            "plasmashell",
            "xfce4-session",
            "xrdp",
            "Xorg",
            "Xwayland",
            "chrome",
            "chromium",
        )
        candidates: list[dict[str, str]] = []

        try:
            proc_entries = list(os.scandir("/proc"))
        except OSError:
            return current

        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat(follow_symlinks=False).st_uid != uid:
                    continue
            except OSError:
                continue

            env = self._read_process_environ(entry.name)
            if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
                continue

            try:
                with open(f"/proc/{entry.name}/comm", "r", encoding="utf-8") as file:
                    name = file.read().strip()
            except OSError:
                name = ""

            score = 0
            if env.get("DISPLAY"):
                score += 2
            if env.get("DBUS_SESSION_BUS_ADDRESS"):
                score += 2
            if env.get("XAUTHORITY"):
                score += 1
            if any(token in name for token in preferred_names):
                score += 3
            env["_score"] = str(score)
            candidates.append(env)

        if not candidates:
            inferred = self._infer_x11_env_from_socket()
            if inferred:
                logger.info("从 X11 socket 推断浏览器环境: DISPLAY=%s", inferred.get("DISPLAY"))
                return inferred
            return current

        best = max(candidates, key=lambda item: int(item.get("_score", "0")))
        desktop_env = {key: value for key in wanted if (value := best.get(key))}
        logger.info(
            "从现有桌面会话补齐浏览器环境: DISPLAY=%s WAYLAND_DISPLAY=%s XDG_SESSION_TYPE=%s",
            desktop_env.get("DISPLAY"),
            desktop_env.get("WAYLAND_DISPLAY"),
            desktop_env.get("XDG_SESSION_TYPE"),
        )
        return desktop_env

    def _infer_x11_env_from_socket(self) -> dict[str, str]:
        """在拿不到会话环境时，根据 /tmp/.X11-unix/X* 兜底推断 X11 DISPLAY。"""
        if sys.platform != "linux":
            return {}

        socket_dir = "/tmp/.X11-unix"
        try:
            entries = [
                entry for entry in os.scandir(socket_dir)
                if entry.name.startswith("X") and entry.name[1:].isdigit()
            ]
        except OSError:
            return {}

        if not entries:
            return {}

        try:
            best = max(entries, key=lambda entry: entry.stat(follow_symlinks=False).st_mtime)
        except OSError:
            return {}

        env = {
            "DISPLAY": f":{best.name[1:]}",
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
        }
        xauthority = os.path.expanduser("~/.Xauthority")
        if os.path.exists(xauthority):
            env["XAUTHORITY"] = xauthority
        return env

    def _browser_process_env(self, headless: bool) -> dict[str, str]:
        env = os.environ.copy()
        local_no_proxy = ["127.0.0.1", "localhost", "::1"]
        for key in ("NO_PROXY", "no_proxy"):
            values = [item.strip() for item in env.get(key, "").split(",") if item.strip()]
            existing = {item.lower() for item in values}
            for host in local_no_proxy:
                if host.lower() not in existing:
                    values.append(host)
            env[key] = ",".join(values)
        if sys.platform == "linux":
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            env.update(self._discover_linux_desktop_env())
            if not headless and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
                raise RuntimeError(
                    "当前后台服务没有可用的 Linux 图形会话环境（DISPLAY/WAYLAND_DISPLAY 为空），"
                    "无法启动可视浏览器。请在远程桌面会话内启动服务，或将 app.browser_headless 改为 true。"
                )
        return env

    def _read_devtools_active_port(self, user_data_dir) -> tuple[int, str] | None:
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

    def _cleanup_stale_singletons(self, user_data_dir) -> None:
        """启动新 Chrome 前清理可能残留的单例锁。

        当上一次 Chrome 进程异常退出（崩溃、kill -9、宿主服务重启）时，
        user-data-dir 下的 SingletonLock/SingletonSocket/SingletonCookie（Linux）
        或 lockfile（部分 Windows 版本）可能残留，导致新进程检测到"已有实例"后
        把命令行转发给一个不存在的进程然后悄悄退出。
        只有在确认本进程没有跟踪到活的 Chrome 子进程时才执行清理，避免误杀。
        """
        if self._chrome_process and self._chrome_process.poll() is None:
            return
        names = ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile")
        for name in names:
            target = user_data_dir / name
            try:
                if target.is_symlink() or target.exists():
                    target.unlink()
                    logger.info("已清理残留单例锁: %s", target)
            except OSError as exc:
                logger.warning("清理残留单例锁 %s 失败: %s", target, exc)

    def _tail_chrome_stderr(self) -> str:
        stderr_path = self.config.log_dir / "chrome-browser.log"
        if not stderr_path.exists():
            return ""
        try:
            with stderr_path.open("rb") as file:
                file.seek(0, os.SEEK_END)
                file_size = file.tell()
                file.seek(max(file_size - 8192, 0))
                stderr_bytes = file.read()
        except Exception:
            return ""
        if not stderr_bytes:
            return ""
        text = stderr_bytes.decode("utf-8", errors="replace").strip()
        lines = text.splitlines()
        return "\n".join(lines[-40:])

    def _find_chrome_executable(self) -> str:
        """寻找系统中真实的 Chrome/Edge 可执行文件路径"""
        config_path = getattr(self.config, 'browser_channel', '')
        if config_path and os.path.isabs(config_path) and os.path.exists(config_path):
            return config_path
        config_alias = config_path.lower() if config_path else ""

        channel_aliases = {
            "chrome": ["google-chrome", "google-chrome-stable"],
            "google-chrome": ["google-chrome", "google-chrome-stable"],
            "chromium": ["chromium-browser", "chromium"],
            "msedge": ["microsoft-edge", "microsoft-edge-stable"],
            "edge": ["microsoft-edge", "microsoft-edge-stable"],
        }
        if config_alias in channel_aliases:
            for command in channel_aliases[config_alias]:
                cmd = shutil.which(command)
                if cmd:
                    return cmd
            
        if sys.platform == "win32":
            paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
            ]
            for p in paths:
                if os.path.exists(p):
                    return p
        elif sys.platform == "darwin":
            paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
            ]
            for p in paths:
                if os.path.exists(p):
                    return p
        else:
            paths = ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "microsoft-edge", "microsoft-edge-stable"]
            for p in paths:
                cmd = shutil.which(p)
                if cmd:
                    return cmd
                    
        raise RuntimeError("无法找到系统中安装的 Chrome 或 Edge 浏览器。请在 config.yaml 中配置 browser_channel 为 chrome、msedge 或绝对路径。")

    async def _ensure_browser(self) -> Page:
        """确保浏览器和页面已经启动。"""
        if self._page and not self._page.is_closed():
            return self._page

        async with self._lock:
            if self._page and not self._page.is_closed():
                return self._page

            logger.info("启动 Playwright 浏览器...")
            if not self._playwright:
                self._ensure_local_proxy_bypass()
                self._playwright = await async_playwright().start()

            # 检测现有浏览器或上下文是否被意外关闭
            if self._context and getattr(self._context, "is_closed", lambda: False)():
                logger.warning("检测到浏览器上下文已关闭，准备重新启动...")
                self._context = None
                self._page = None
                
            if self._browser and not getattr(self._browser, "is_connected", lambda: True)():
                logger.warning("检测到浏览器已断开连接，准备重新启动...")
                self._browser = None
                self._context = None
                self._page = None

            if not self._context:
                headless = self.config.browser_headless
                user_data_dir = self.config.base_dir / ".browser_data"
                user_data_dir.mkdir(parents=True, exist_ok=True)
                
                try:
                    cdp_endpoint = ""

                    # ── 优先尝试复用已有的 Chrome 进程 ──────────────────────────────────
                    # 若 _chrome_process 仍在运行（poll() is None）且记录了端口，先尝试直接连接，
                    # 避免因 _context 意外置 None 而再次拉起新进程（会导致桌面出现多个浏览器窗口）。
                    existing_port = getattr(self, "_chrome_cdp_port", None)
                    if existing_port and self._chrome_process and self._chrome_process.poll() is None:
                        logger.info("检测到已有 Chrome 进程（port=%s），尝试复用 CDP 连接...", existing_port)
                        cdp_endpoint = self._get_cdp_websocket_url(existing_port) or ""
                        if cdp_endpoint:
                            logger.info("成功复用已有 Chrome 进程 CDP 端口 %s。", existing_port)
                        else:
                            logger.warning("已有 Chrome 进程 CDP 端口 %s 不可达，将重新启动浏览器。", existing_port)
                            # 旧进程不可达则终止它，下面会重新启动
                            try:
                                self._chrome_process.terminate()
                                self._chrome_process.wait(timeout=3)
                            except Exception:
                                self._chrome_process.kill()
                            self._chrome_process = None
                            self._chrome_cdp_port = None
                            if self._chrome_stderr_file:
                                self._chrome_stderr_file.close()
                                self._chrome_stderr_file = None

                    # ── 若没有可复用的进程，则启动新的 Chrome ─────────────────────────
                    if not cdp_endpoint:
                        chrome_path = self._find_chrome_executable()
                        port = self._find_free_port()
                        browser_env = self._browser_process_env(headless)

                        # 启动前清理可能残留的单例锁，否则新进程会被旧锁挡住
                        # 自动转发命令行后悄悄退出，导致 --remote-debugging-port 永远不生效。
                        self._cleanup_stale_singletons(user_data_dir)

                        window_size = self.config.browser_window_size
                        if not window_size:
                            window_size = f"{random.randint(1366, 1920)},{random.randint(768, 1080)}"
                            
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
                        
                        if self.config.browser_user_agent:
                            args.append(f"--user-agent={self.config.browser_user_agent}")
                            
                        if sys.platform == "linux":
                            args.extend([
                                "--no-sandbox",
                                "--disable-gpu",
                                "--disable-blink-features=AutomationControlled",
                            ])
                        if headless:
                            args.extend(["--headless=new"])
                            
                        logger.info(
                            "以 CDP 接管模式启动原生浏览器: path=%s port=%s headless=%s display=%s wayland=%s",
                            chrome_path,
                            port,
                            headless,
                            browser_env.get("DISPLAY"),
                            browser_env.get("WAYLAND_DISPLAY"),
                        )
                        chrome_stderr_path = self.config.log_dir / "chrome-browser.log"
                        chrome_stderr_path.parent.mkdir(parents=True, exist_ok=True)
                        # 每次启动覆盖写，避免历次 stderr 累积让排障时分不清是哪次启动
                        self._chrome_stderr_file = chrome_stderr_path.open("wb", buffering=0)
                        self._chrome_process = subprocess.Popen(
                            args,
                            stdout=subprocess.DEVNULL,
                            stderr=self._chrome_stderr_file,
                            env=browser_env,
                        )
                        # 记录本次使用的 CDP 端口，供后续复用检测
                        self._chrome_cdp_port = port
                        
                        # 等待调试端口就绪
                        cdp_wait_seconds = self.config.browser_cdp_wait_seconds
                        deadline = asyncio.get_running_loop().time() + cdp_wait_seconds
                        while asyncio.get_running_loop().time() < deadline:
                            if self._chrome_process.poll() is not None:
                                stderr_tail = self._tail_chrome_stderr()
                                detail = f"\nChrome stderr:\n{stderr_tail}" if stderr_tail else ""
                                raise RuntimeError(
                                    f"Chrome 进程已退出，无法建立 CDP 连接（退出码 {self._chrome_process.returncode}）。{detail}"
                                )
                            # 优先从 DevToolsActivePort 读实际端口（Chrome 可能因端口冲突
                            # 回落到随机端口，这时请求的 port 永远等不到）。
                            if dev := self._read_devtools_active_port(user_data_dir):
                                actual_port, _ = dev
                                if ws := self._get_cdp_websocket_url(actual_port):
                                    cdp_endpoint = ws
                                    self._chrome_cdp_port = actual_port
                                    if actual_port != port:
                                        logger.info(
                                            "Chrome 实际 CDP 端口 %d 与请求端口 %d 不同（端口冲突回落），已自动适配。",
                                            actual_port, port,
                                        )
                                    break
                            # 回退：仍试请求端口（兼容 DevToolsActivePort 未写完或不存在）
                            if websocket_url := self._get_cdp_websocket_url(port):
                                cdp_endpoint = websocket_url
                                break
                            await asyncio.sleep(0.5)

                        if not cdp_endpoint:
                            stderr_tail = self._tail_chrome_stderr()
                            detail = f"\nChrome stderr:\n{stderr_tail}" if stderr_tail else ""
                            raise RuntimeError(
                                f"等待浏览器 CDP 端口 {self._chrome_cdp_port or port} 就绪超时（已等待 {cdp_wait_seconds}s）。"
                                "请查看 Chrome stderr 判断是否为显示环境、权限、沙箱或 profile 锁定问题；"
                                "若冷启动较慢可调高 app.browser_cdp_wait_seconds。"
                                f"{detail}"
                            )
                    
                    # Playwright 接管（无论是复用还是新启动）
                    self._browser = await self._playwright.chromium.connect_over_cdp(cdp_endpoint)
                    
                    if self._browser.contexts:
                        self._context = self._browser.contexts[0]
                    else:
                        raise RuntimeError("连接到 CDP 成功，但未找到可用的 BrowserContext。")

                except Exception as e:
                    logger.error("以 CDP 模式启动或接管浏览器失败: %s", e)
                    if self._chrome_process:
                        self._chrome_process.kill()
                        self._chrome_process = None
                    self._chrome_cdp_port = None
                    if self._chrome_stderr_file:
                        self._chrome_stderr_file.close()
                        self._chrome_stderr_file = None
                    raise

            try:
                pages = self._context.pages
                valid_pages = [p for p in pages if not (".top-chrome" in p.url or "chrome-extension://" in p.url)] if pages else []
                if valid_pages:
                    # 智能标签页选择：优先选择用户当前正在看（visible）的标签页
                    active_page = None
                    for p in reversed(valid_pages):
                        try:
                            # 如果是一个空的新标签页，或者处于激活状态
                            state = await p.evaluate('document.visibilityState')
                            if state == 'visible':
                                active_page = p
                                break
                        except Exception:
                            pass
                            
                    self._page = active_page if active_page else valid_pages[-1]
                else:
                    self._page = await self._context.new_page()
                    
                # 注入 stealth_async 以抹除自动化特征
                try:
                    await Stealth().apply_stealth_async(self._page)
                except Exception as e:
                    logger.warning("为页面注入 stealth 脚本时发生错误: %s", e)
                    
                logger.info("Playwright 浏览器已启动并绑定到活动页面。")
            except PlaywrightError as e:
                if "closed" in str(e).lower():
                    logger.warning("尝试获取/创建页面失败，因为上下文已关闭。准备完全重启浏览器: %s", e)
                    self._context = None
                    self._browser = None
                    self._page = None
                    # 递归重试一次
                    return await self._ensure_browser()
                raise e

            return self._page

    async def close(self) -> None:
        """关闭浏览器实例。"""
        # 先获取 _op_lock 等待所有进行中的操作收尾，
        # 防止 idle monitor 在 click/get_state 等操作中途把 _page 置 None。
        # 锁顺序必须是 _op_lock → _lock，与公开操作方法（间接通过 _ensure_browser 拿 _lock）一致，避免死锁。
        async with self._op_lock:
            async with self._lock:
                if self._page:
                    try:
                        await self._page.close()
                    except Exception:
                        pass
                    self._page = None
                if self._context:
                    try:
                        await self._context.close()
                    except Exception:
                        pass
                    self._context = None
                if self._browser:
                    try:
                        await self._browser.close()
                    except Exception:
                        pass
                    self._browser = None
                if self._playwright:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None

                if self._chrome_process:
                    try:
                        if self._chrome_process.poll() is None:
                            logger.info("终止后台 Chrome 原生进程...")
                            self._chrome_process.terminate()
                            try:
                                self._chrome_process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                self._chrome_process.kill()
                    except Exception as e:
                        logger.error("关闭 Chrome 进程失败: %s", e)
                    finally:
                        self._chrome_process = None
                # 清除 CDP 端口记录，避免下次 _ensure_browser 误用已失效端口
                self._chrome_cdp_port = None
                if self._chrome_stderr_file:
                    try:
                        self._chrome_stderr_file.close()
                    except Exception:
                        pass
                    self._chrome_stderr_file = None

                logger.info("Playwright 浏览器已关闭。")

