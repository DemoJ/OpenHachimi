"""Lifecycle management for the Playwright browser（启动编排层）。

分层职责：
- CDP 探测（异步 socket）见 ``cdp.py``
- Chrome 进程/参数/单例锁/进程组终止见 ``chrome_process.py``
- Linux 桌面环境发现见 ``env_discovery.py``

启动策略：
1. 配置了 ``browser_connect_url`` 时直接接管外部调试实例，不自行拉起进程；
2. 否则优先复用本进程已拉起的 Chrome（按记录的 CDP 端口探测可达性）；
3. 都没有则分层启动：进程拉起 → CDP 就绪 → connect 接管 → 页面绑定，
   前三个阶段任一失败都会清理现场并按指数退避重试（合计最多 3 次），
   全程非阻塞轮询，不会冻结事件循环。
"""

import asyncio
import logging
import os
import random
import subprocess
import sys

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright_stealth import Stealth

from openhachimi_agent.core.config import AppConfig
from .cdp import fetch_cdp_websocket_url, find_free_port
from .chrome_process import (
    build_launch_args,
    cleanup_stale_singletons,
    find_chrome_executable,
    read_devtools_active_port,
    tail_chrome_stderr,
    terminate_browser_process,
)
from .env_discovery import browser_process_env

logger = logging.getLogger(__name__)

_LAUNCH_MAX_ATTEMPTS = 3
_LAUNCH_RETRY_BASE_DELAY = 1.0
_CDP_PROBE_TIMEOUT = 1.0


def _stderr_detail(log_dir) -> str:
    stderr_tail = tail_chrome_stderr(log_dir)
    return f"\nChrome stderr:\n{stderr_tail}" if stderr_tail else ""


def _startup_hints() -> str:
    return (
        "请检查：1) 桌面是否登录了图形会话且未锁屏（锁屏/注销会导致窗口无法创建，"
        "请先在 Ubuntu 桌面登录/解锁一次 Wayland 会话，或将 app.browser_headless 改为 true）；"
        "2) 沙箱/权限/磁盘空间问题；3) 是否残留旧 Chrome 进程占用 profile；"
        "4) 冷启动较慢可调高 app.browser_cdp_wait_seconds。"
    )


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

    async def _ensure_browser(self) -> Page:
        """确保浏览器和页面已经启动（全程持有 _lock，串行化生命周期）。"""
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

            # 同一把锁内循环：页面绑定期间上下文被关闭时，清理后重新走完整启动。
            # 用循环替代递归调用，避免 asyncio.Lock（不可重入）递归加锁死锁。
            while not self._context:
                try:
                    self._browser = await self._bootstrap_browser()
                    if not self._browser.contexts:
                        raise RuntimeError("连接到 CDP 成功，但未找到可用的 BrowserContext。")
                    self._context = self._browser.contexts[0]
                except asyncio.CancelledError:
                    # 工具调用被上层取消（如 streaming 总超时）时同样要清理现场，
                    # 否则 Chrome 进程会泄漏在后台。CancelledError 是 BaseException，
                    # 不能被下面的 except Exception 捕获。
                    logger.warning("浏览器启动被取消，清理现场...")
                    await self._abort_startup()
                    raise
                except Exception as exc:
                    logger.error("以 CDP 模式启动或接管浏览器失败: %s", exc)
                    await self._abort_startup()
                    raise

                try:
                    self._page = await self._bind_active_page()
                    return self._page
                except PlaywrightError as exc:
                    if "closed" not in str(exc).lower():
                        raise
                    logger.warning("页面绑定期间上下文被关闭，重新启动浏览器: %s", exc)
                    self._browser = None
                    self._context = None
                    self._page = None

            return self._page

    async def _bootstrap_browser(self) -> Browser:
        """按优先级获取一个已连接的 Browser：外部 CDP → 复用进程 → 新建进程。"""
        connect_url = (getattr(self.config, "browser_connect_url", "") or "").strip()
        if connect_url:
            logger.info("[接管] 使用配置的外部 CDP 地址: %s", connect_url)
            return await self._connect_with_retry(connect_url)

        existing_port = getattr(self, "_chrome_cdp_port", None)
        if existing_port and self._chrome_process and self._chrome_process.poll() is None:
            logger.info("[复用] 检测到已有 Chrome 进程（port=%s），尝试复用 CDP 连接...", existing_port)
            ws_url = await fetch_cdp_websocket_url(existing_port, timeout=_CDP_PROBE_TIMEOUT)
            if ws_url:
                logger.info("[复用] 成功复用已有 Chrome 进程 CDP 端口 %s。", existing_port)
                return await self._playwright.chromium.connect_over_cdp(ws_url)
            logger.warning("[复用] 已有 Chrome 进程 CDP 端口 %s 不可达，将重新启动浏览器。", existing_port)
            await self._cleanup_chrome_process()

        headless = self.config.browser_headless
        user_data_dir = self.config.base_dir / ".browser_data"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        return await self._launch_and_connect(headless, user_data_dir)

    async def _connect_with_retry(self, cdp_endpoint: str, attempts: int = 2) -> Browser:
        """连接外部 CDP 地址，失败重试（外部浏览器可能尚未就绪）。"""
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._playwright.chromium.connect_over_cdp(cdp_endpoint)
            except Exception as exc:
                last_err = exc
                logger.warning("连接外部 CDP 失败（第 %d/%d 次）: %s", attempt, attempts, exc)
                if attempt < attempts:
                    await asyncio.sleep(1.0)
        raise RuntimeError(f"连接外部 CDP 地址失败: {last_err}") from last_err

    async def _launch_and_connect(self, headless: bool, user_data_dir) -> Browser:
        """分层启动 Chrome：单次失败清理现场后指数退避重试（合计最多 3 次）。"""
        wait_seconds = self.config.browser_cdp_wait_seconds
        last_err: Exception | None = None
        for attempt in range(1, _LAUNCH_MAX_ATTEMPTS + 1):
            logger.info(
                "[1/3] 启动 Chrome 进程（第 %d/%d 次尝试，headless=%s）...",
                attempt, _LAUNCH_MAX_ATTEMPTS, headless,
            )
            try:
                browser = await asyncio.wait_for(
                    self._launch_chrome_once(headless, user_data_dir, wait_seconds),
                    timeout=wait_seconds + 5,
                )
                return browser
            except asyncio.CancelledError:
                # 外层取消（如 streaming 总超时）时也必须回收 Chrome 进程，
                # 否则会残留无主浏览器进程。
                logger.warning("Chrome 启动被取消，清理进程...")
                await self._cleanup_chrome_process()
                raise
            except Exception as exc:
                last_err = exc
                await self._cleanup_chrome_process()
                if attempt < _LAUNCH_MAX_ATTEMPTS:
                    delay = _LAUNCH_RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    logger.warning(
                        "[启动失败] 第 %d 次尝试: %s。%.1fs 后重试（合计最多 %d 次）...",
                        attempt, exc, delay, _LAUNCH_MAX_ATTEMPTS,
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"Chrome 启动失败（已重试 {_LAUNCH_MAX_ATTEMPTS} 次）: {last_err}"
        ) from last_err

    async def _launch_chrome_once(self, headless: bool, user_data_dir, wait_seconds: int) -> Browser:
        """单次完整启动：拉起进程 → 等待 CDP 就绪 → Playwright 接管。"""
        chrome_path = find_chrome_executable(self.config)
        port = find_free_port()
        browser_env = browser_process_env(headless)

        # 启动前清理可能残留的单例锁，否则新进程会被旧锁挡住
        # 自动转发命令行后悄悄退出，导致 --remote-debugging-port 永远不生效。
        cleanup_stale_singletons(user_data_dir)

        window_size = self.config.browser_window_size or (
            f"{random.randint(1366, 1920)},{random.randint(768, 1080)}"
        )
        args = build_launch_args(self.config, chrome_path, port, user_data_dir, window_size, headless)

        chrome_stderr_path = self.config.log_dir / "chrome-browser.log"
        chrome_stderr_path.parent.mkdir(parents=True, exist_ok=True)
        # 每次启动覆盖写，避免历次 stderr 累积让排障时分不清是哪次启动
        self._chrome_stderr_file = chrome_stderr_path.open("wb", buffering=0)
        # start_new_session 使 Chrome 成为会话组长，进程组终止可连带 zygote/renderer 子进程
        self._chrome_process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=self._chrome_stderr_file,
            env=browser_env,
            start_new_session=sys.platform != "win32",
        )
        # 记录本次使用的 CDP 端口，供后续复用检测
        self._chrome_cdp_port = port

        logger.info(
            "[2/3] Chrome 进程已拉起(pid=%d port=%s display=%s wayland=%s)，等待 CDP 端口就绪...",
            self._chrome_process.pid,
            port,
            browser_env.get("DISPLAY"),
            browser_env.get("WAYLAND_DISPLAY"),
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        cdp_endpoint = ""
        while loop.time() < deadline:
            if self._chrome_process.poll() is not None:
                raise RuntimeError(
                    f"Chrome 进程已退出，无法建立 CDP 连接（退出码 {self._chrome_process.returncode}）。"
                    f"{_stderr_detail(self.config.log_dir)}"
                )
            # 优先从 DevToolsActivePort 读实际端口（Chrome 可能因端口冲突
            # 回落到随机端口，这时请求的 port 永远等不到）。
            if dev := read_devtools_active_port(user_data_dir):
                actual_port, _ = dev
                if ws := await fetch_cdp_websocket_url(actual_port, timeout=_CDP_PROBE_TIMEOUT):
                    cdp_endpoint = ws
                    self._chrome_cdp_port = actual_port
                    if actual_port != port:
                        logger.info(
                            "Chrome 实际 CDP 端口 %d 与请求端口 %d 不同（端口冲突回落），已自动适配。",
                            actual_port, port,
                        )
                    break
            # 回退：仍试请求端口（兼容 DevToolsActivePort 未写完或不存在）
            if ws := await fetch_cdp_websocket_url(port, timeout=_CDP_PROBE_TIMEOUT):
                cdp_endpoint = ws
                break
            await asyncio.sleep(0.5)

        if not cdp_endpoint:
            raise RuntimeError(
                f"等待浏览器 CDP 端口 {self._chrome_cdp_port or port} 就绪超时（已等待 {wait_seconds}s）。"
                f"{_startup_hints()}{_stderr_detail(self.config.log_dir)}"
            )

        logger.info("[3/3] CDP 端口就绪(port=%s)，Playwright 接管浏览器...", self._chrome_cdp_port)
        return await self._playwright.chromium.connect_over_cdp(cdp_endpoint)

    async def _bind_active_page(self) -> Page:
        """绑定当前 context 的活动页面（无页面则新建），并注入 stealth。"""
        pages = self._context.pages
        valid_pages = [p for p in pages if not (".top-chrome" in p.url or "chrome-extension://" in p.url)] if pages else []
        if valid_pages:
            # 智能标签页选择：优先选择用户当前正在看（visible）的标签页
            active_page = None
            for p in reversed(valid_pages):
                try:
                    # 如果是一个空的新标签页，或者处于激活状态
                    state = await p.evaluate("document.visibilityState")
                    if state == "visible":
                        active_page = p
                        break
                except Exception:
                    pass
            page = active_page if active_page else valid_pages[-1]
        else:
            page = await self._context.new_page()

        # 注入 stealth 以抹除自动化特征
        try:
            await Stealth().apply_stealth_async(page)
        except Exception as exc:
            logger.warning("为页面注入 stealth 脚本时发生错误: %s", exc)

        logger.info("Playwright 浏览器已启动并绑定到活动页面。")
        return page

    async def _cleanup_chrome_process(self) -> None:
        """清理 Chrome 进程与端口记录（不触碰 playwright 驱动器，供启动重试用）。"""
        if self._chrome_process:
            proc, self._chrome_process = self._chrome_process, None
            self._chrome_cdp_port = None
            try:
                await asyncio.to_thread(terminate_browser_process, proc)
            except Exception as exc:
                logger.debug("终止 Chrome 进程失败: %s", exc)
        if self._chrome_stderr_file:
            try:
                self._chrome_stderr_file.close()
            except Exception:
                pass
            self._chrome_stderr_file = None

    async def _abort_startup(self) -> None:
        """启动失败全量清理：进程组 + stderr + playwright 驱动器 + CDP 端口。"""
        await self._cleanup_chrome_process()
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.debug("停止 playwright 驱动器失败: %s", exc)
            self._playwright = None

    async def close(self) -> None:
        """关闭浏览器实例（先等操作收尾，再关闭资源与 Chrome 进程）。"""
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
                    proc = self._chrome_process
                    try:
                        if proc.poll() is None:
                            logger.info("终止后台 Chrome 原生进程...")
                            await asyncio.to_thread(terminate_browser_process, proc)
                    except Exception as exc:
                        logger.error("关闭 Chrome 进程失败: %s", exc)
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