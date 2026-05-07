"""Browser 管理服务，用于提供 Playwright 支持和可访问性树截取。"""

import asyncio
import logging
import random
import os
import socket
import sys
import shutil
import subprocess
import urllib.request
import urllib.error
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright_stealth import Stealth

from openhachimi_agent.core.config import AppConfig

logger = logging.getLogger(__name__)


class BrowserManager:
    """管理后台 Playwright 浏览器实例。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._chrome_process = None
        self._chrome_stderr_file = None
        self._lock = asyncio.Lock()
        
        # 存储当前页面的可交互元素映射表：id -> locator
        # 这样 LLM 只需要返回一个数字 ID 就能点击
        self._element_mapping: dict[int, str] = {}

    def _find_free_port(self) -> int:
        """让系统分配一个当前可用的本地端口，避免固定 9222 端口冲突。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

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
        if sys.platform == "linux":
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            env.update(self._discover_linux_desktop_env())
            if not headless and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
                raise RuntimeError(
                    "当前后台服务没有可用的 Linux 图形会话环境（DISPLAY/WAYLAND_DISPLAY 为空），"
                    "无法启动可视浏览器。请在远程桌面会话内启动服务，或将 app.browser_headless 改为 true。"
                )
        return env

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
                    chrome_path = self._find_chrome_executable()
                    port = self._find_free_port()
                    browser_env = self._browser_process_env(headless)
                    
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
                        "--window-size=1280,900",
                    ]
                    if sys.platform == "linux":
                        args.extend([
                            "--no-sandbox",
                            "--disable-gpu",
                            "--enable-automation",
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
                    self._chrome_stderr_file = chrome_stderr_path.open("ab", buffering=0)
                    self._chrome_process = subprocess.Popen(
                        args,
                        stdout=subprocess.DEVNULL,
                        stderr=self._chrome_stderr_file,
                        env=browser_env,
                    )
                    
                    # 等待调试端口就绪
                    max_retries = 30
                    port_ready = False
                    for _ in range(max_retries):
                        if self._chrome_process.poll() is not None:
                            stderr_tail = self._tail_chrome_stderr()
                            detail = f"\nChrome stderr:\n{stderr_tail}" if stderr_tail else ""
                            raise RuntimeError(
                                f"Chrome 进程已退出，无法建立 CDP 连接（退出码 {self._chrome_process.returncode}）。{detail}"
                            )
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                                if response.getcode() == 200:
                                    port_ready = True
                                    break
                        except (urllib.error.URLError, ConnectionError):
                            pass
                        await asyncio.sleep(0.5)
                        
                    if not port_ready:
                        stderr_tail = self._tail_chrome_stderr()
                        detail = f"\nChrome stderr:\n{stderr_tail}" if stderr_tail else ""
                        raise RuntimeError(
                            f"等待浏览器 CDP 端口 {port} 就绪超时。"
                            "请查看 Chrome stderr 判断是否为显示环境、权限、沙箱或 profile 锁定问题。"
                            f"{detail}"
                        )
                        
                    # Playwright 接管
                    self._browser = await self._playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    
                    if self._browser.contexts:
                        self._context = self._browser.contexts[0]
                    else:
                        raise RuntimeError("连接到 CDP 成功，但未找到可用的 BrowserContext。")

                except Exception as e:
                    logger.error("以 CDP 模式启动或接管浏览器失败: %s", e)
                    if self._chrome_process:
                        self._chrome_process.kill()
                        self._chrome_process = None
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

    async def _update_active_page(self):
        """自动切换到最新标签页，并强制将其置于前台显示。"""
        if self._context and self._context.pages:
            valid_pages = [p for p in self._context.pages if not (".top-chrome" in p.url or "chrome-extension://" in p.url)]
            if valid_pages:
                newest_page = valid_pages[-1]
                if self._page != newest_page:
                    logger.info("检测到新标签页，自动切换当前页面。")
                    self._page = newest_page
                
            try:
                # 每次执行动作前，强制把 Agent 正在操作的标签页切到最前面，防止在后台默默执行
                await self._page.bring_to_front()
            except Exception:
                pass

    async def navigate(self, url: str) -> str:
        """导航到指定网页。"""
        if not url.startswith("http"):
            url = "https://" + url
            
        page = await self._ensure_browser()
        await self._update_active_page()
        page = self._page
        
        # 避免重复加载同一页面
        current_url = page.url.rstrip("/")
        target_url = url.rstrip("/")
        if current_url == target_url or current_url.startswith(target_url + "?") or current_url.startswith(target_url + "#"):
            logger.info("Browser already at target url: %s", current_url)
            return f"当前已在 {page.url}，无需重复导航。请直接使用 browser_get_state 获取页面内容。"
            
        logger.info("Browser navigating to: %s", url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return f"成功导航到：{page.url}。请使用 browser_get_state 获取页面内容。"
        except Exception as e:
            logger.error("Navigation failed: %s", e)
            return f"导航失败：{e}"

    async def get_state(self) -> str:
        """获取当前页面的完整可访问性树（包含元素 ID），供大模型阅读。

        【重要设计原则】
        - 返回整个已渲染页面的所有元素，不受当前滚动位置限制。
        - 视口内外的元素均会输出，用位置标记（[视口内]/[↑上方]/[↓下方]）区分。
        - browser_scroll 应仅用于触发懒加载（无限滚动），而非"查看"已有内容。
        """
        await self._update_active_page()
        
        if not self._page or self._page.is_closed():
            return "当前没有打开的页面，请先使用 browser_navigate 导航到网页。"

        logger.info("获取当前页面状态（Accessibility Tree）...")
        
        # 重置映射表
        self._element_mapping = {}
        
        try:
            await asyncio.sleep(0.5)
        except Exception:
            pass
            
        # 单次返回全页元素的上限，防止超大页面撑爆模型上下文
        MAX_ELEMENTS = 500

        try:
            script = """
            (maxElements) => {
                let idCounter = 1;
                const elements = [];
                const interactiveNodes = [];
                
                const nodes = document.querySelectorAll('*');
                const winHeight = window.innerHeight;
                const winWidth  = window.innerWidth;
                
                for (const node of nodes) {
                    if (elements.length >= maxElements) break;
                    
                    // 1. 过滤无意义标签
                    const tagName = node.tagName.toLowerCase();
                    if (['script','style','noscript','meta','link','head'].includes(tagName)) continue;
                    
                    // 2. 尺寸过滤：零尺寸 = 未渲染，直接跳过
                    const rect = node.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    
                    // 3. CSS 可见性过滤
                    const style = window.getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                    
                    // 4. 计算元素相对视口的位置（用于输出标记，不用于过滤）
                    let position;
                    if (rect.bottom < 0)       position = 'above';    // 视口上方
                    else if (rect.top > winHeight) position = 'below'; // 视口下方
                    else                           position = 'viewport'; // 当前可见
                    
                    // 5. 交互性检测
                    const isEditable = tagName === 'input' || tagName === 'textarea' ||
                                       node.isContentEditable ||
                                       node.getAttribute('role') === 'textbox' ||
                                       node.getAttribute('role') === 'combobox';
                                       
                    let isInteractive = isEditable || tagName === 'a' || tagName === 'button' || tagName === 'select' ||
                                        node.getAttribute('role') === 'button' || node.getAttribute('role') === 'link' ||
                                        node.getAttribute('role') === 'menuitem' || node.getAttribute('role') === 'option' ||
                                        (node.hasAttribute('tabindex') && node.getAttribute('tabindex') !== '-1') ||
                                        style.cursor === 'pointer' || style.cursor === 'text';
                    
                    // 6. 物理遮挡剔除（仅对视口内元素有意义，视口外无浮层覆盖问题）
                    if (isInteractive && position === 'viewport') {
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top  + rect.height / 2;
                        if (centerX >= 0 && centerX <= winWidth && centerY >= 0 && centerY <= winHeight) {
                            const topEl = document.elementFromPoint(centerX, centerY);
                            if (topEl && topEl !== node && !node.contains(topEl) && !topEl.contains(node)) {
                                let p1 = node, common = null, depth = 0;
                                while (p1 && depth < 5) {
                                    if (p1.contains(topEl)) { common = p1; break; }
                                    p1 = p1.parentElement;
                                    depth++;
                                }
                                if (!common) isInteractive = false;
                            }
                        }
                    }
                    
                    // 7. 文本提取
                    let text = '';
                    if (isInteractive) {
                        if (isEditable) {
                            let val = (node.value || node.innerText || '').trim();
                            if (!val) val = node.getAttribute('placeholder') || node.getAttribute('aria-label') || node.getAttribute('data-testid') || '';
                            text = val;
                        } else {
                            text = node.getAttribute('aria-label') || node.getAttribute('alt') || node.innerText || node.value || node.getAttribute('data-testid') || '';
                        }
                    } else {
                        // 非交互节点只取直属文本，避免父子俄罗斯套娃
                        let directText = '';
                        for (let child of node.childNodes) {
                            if (child.nodeType === 3) directText += child.textContent;
                        }
                        directText = directText.trim();
                        if (directText) {
                            text = node.getAttribute('aria-label') || node.getAttribute('alt') || directText;
                        } else {
                            continue;
                        }
                    }
                    
                    text = text.replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim();
                    if (text.length > 120) text = text.substring(0, 120) + '...';
                    if (!text && !isInteractive) continue;
                    
                    // 8. 祖先去重
                    if (!isInteractive) {
                        if (interactiveNodes.some(parent => parent.contains(node))) continue;
                    }
                    
                    const role = node.getAttribute('role') || tagName;
                    const elData = {
                        id: idCounter++,
                        tag: tagName,
                        role: role,
                        text: text,
                        type: node.type || undefined,
                        isInteractive: isInteractive,
                        position: position,
                    };
                    
                    node.setAttribute('data-agent-id', elData.id);
                    elements.push(elData);
                    if (isInteractive) interactiveNodes.push(node);
                }
                
                return {
                    url: document.location.href,
                    title: document.title,
                    elements: elements,
                    truncated: elements.length >= maxElements,
                    scrollY: Math.round(window.scrollY),
                    scrollHeight: Math.round(document.body.scrollHeight),
                    clientHeight: Math.round(window.innerHeight),
                };
            }
            """
            
            result = await self._page.evaluate(script, MAX_ELEMENTS)
            
            scroll_y  = result.get('scrollY', 0)
            scroll_h  = result.get('scrollHeight', 0)
            client_h  = result.get('clientHeight', 0)
            truncated = result.get('truncated', False)
            
            output = [f"URL: {result['url']}"]
            output.append(f"Title: {result['title']}")
            
            # 统计各区域元素数
            all_els = result["elements"]
            cnt_above    = sum(1 for e in all_els if e.get('position') == 'above')
            cnt_viewport = sum(1 for e in all_els if e.get('position') == 'viewport')
            cnt_below    = sum(1 for e in all_els if e.get('position') == 'below')
            output.append(
                f"[页面概况] 共 {len(all_els)} 个元素"
                + (f"（已达上限 {MAX_ELEMENTS}，页面可能有更多）" if truncated else "")
                + f"：视口上方 {cnt_above} 个 | 视口内 {cnt_viewport} 个 | 视口下方 {cnt_below} 个"
            )
            
            # 滚动提示：区分"已有内容"与"待懒加载内容"
            # 判断依据：如果页面总高度远超已知元素覆盖的区域，说明可能有懒加载内容
            loaded_bottom = scroll_y + scroll_h  # 粗略估计
            potential_lazy = scroll_h > client_h * 2 and cnt_below == 0 and not truncated
            if potential_lazy:
                output.append(
                    f"[滚动提示] 页面总高度 {scroll_h}px，当前视口 {client_h}px。"
                    "若需要加载更多内容（如无限滚动列表），可使用 browser_scroll('down') 触发懒加载后重新 browser_get_state。"
                )
            elif cnt_below > 0 or cnt_above > 0:
                output.append(
                    "[滚动提示] 以上已包含整个页面的全部已渲染元素（含视口外），无需滚动即可阅读全部内容。"
                    "仅当需要触发懒加载（如无限滚动）时才使用 browser_scroll。"
                )
            else:
                output.append("[滚动提示] 当前页面所有内容均已包含在上方列表中。")
            
            output.append("-" * 40)
            output.append("页面元素列表（[*] = 可交互，[↑] = 视口上方，[↓] = 视口下方）：")
            
            for el in all_els:
                self._element_mapping[el['id']] = f"[data-agent-id='{el['id']}']"
                
                type_str     = f" [type:{el['type']}]" if el.get('type') else ""
                interact_mark = " [*]" if el.get('isInteractive') else ""
                pos = el.get('position', 'viewport')
                pos_mark = "" if pos == 'viewport' else (" [↑]" if pos == 'above' else " [↓]")
                output.append(f"[{el['id']}]{interact_mark}{pos_mark} {el['role'].upper()}{type_str}: {el['text']}")
                
            return "\n".join(output)
            
        except Exception as e:
            logger.error("Failed to get state: %s", e)
            return f"获取页面状态失败：{e}"


    async def click(self, element_id: int) -> str:
        """点击指定 ID 的元素。"""
        if not self._page or self._page.is_closed():
            return "当前没有打开的页面。"
            
        if element_id not in self._element_mapping:
            return f"找不到 ID 为 {element_id} 的元素，请先调用 browser_get_state 刷新状态。"
            
        selector = self._element_mapping[element_id]
        logger.info("Browser clicking element_id=%d selector=%s", element_id, selector)
        
        try:
            # 模拟人类：在点击前稍微停顿
            await asyncio.sleep(random.uniform(0.2, 0.6))
            # 尝试点击，如果被遮挡，重试使用 force=True。增加随机按压延迟模拟人类
            try:
                await self._page.locator(selector).first.click(timeout=5000, delay=random.randint(10, 50))
            except Exception:
                await self._page.locator(selector).first.click(timeout=3000, force=True, delay=random.randint(10, 50))
            # 等待可能发生的页面跳转或加载
            try:
                # 等待一会儿让新标签页有机会打开，或者让现代框架完成DOM渲染
                await asyncio.sleep(1.0)
                await self._update_active_page()
            except Exception:
                pass
            return f"成功点击元素 [{element_id}]。"
        except Exception as e:
            return f"点击失败：{e}"

    async def type_text(self, element_id: int, text: str) -> str:
        """在指定 ID 的输入框中输入文本。"""
        await self._update_active_page()
        
        if not self._page or self._page.is_closed():
            return "当前没有打开的页面。"
            
        if element_id not in self._element_mapping:
            return f"找不到 ID 为 {element_id} 的元素，请先调用 browser_get_state 刷新状态。"
            
        selector = self._element_mapping[element_id]
        logger.info("Browser typing text in element_id=%d selector=%s", element_id, selector)
        
        try:
            locator = self._page.locator(selector).first
            # 模拟人类交互：先点击聚焦框，稍微等待
            try:
                await locator.click(timeout=5000)
            except Exception:
                await locator.click(timeout=3000, force=True)
            await asyncio.sleep(random.uniform(0.1, 0.4))
            
            # 检查是否是 Mac (用于全选快捷键)
            is_mac = sys.platform == "darwin"
            modifier = "Meta" if is_mac else "Control"
            
            # 尝试用全选+删除来清空内容，这对特殊的 contenteditable 元素更友好
            try:
                # 只在元素内部全选，或者如果 locator.fill 报错，至少这是一种 fallback
                # 注意：某些情况下 click() 后焦点已经在里面了
                await self._page.keyboard.press(f"{modifier}+A")
                await self._page.keyboard.press("Backspace")
                await asyncio.sleep(0.1)
            except Exception:
                pass
                
            # 尝试逐字输入文本，使用较小的随机延迟加快输入速度但保持人类特征
            await locator.press_sequentially(text, delay=random.randint(10, 30), timeout=10000)
            return f"成功在元素 [{element_id}] 输入文本。"
        except Exception as e:
            return f"输入文本失败：{e}"

    async def scroll(self, direction: str, amount: int = 600) -> str:
        """滚动页面。
        
        direction: 滚动方向，支持 'up'（向上）、'down'（向下）、'top'（跳到页首）、'bottom'（跳到页尾）
        amount: 滚动像素数（仅 up/down 有效，默认 600，约一屏高度）
        """
        await self._update_active_page()
        
        if not self._page or self._page.is_closed():
            return "当前没有打开的页面。"
        
        direction = direction.strip().lower()
        if direction not in ("up", "down", "top", "bottom"):
            return f"不支持的滚动方向：{direction}，请使用 up / down / top / bottom。"
        
        try:
            if direction == "top":
                await self._page.evaluate("window.scrollTo(0, 0)")
                result_msg = "已滚动到页面顶部。"
            elif direction == "bottom":
                await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                result_msg = "已滚动到页面底部。"
            elif direction == "down":
                await self._page.evaluate(f"window.scrollBy(0, {amount})")
                result_msg = f"已向下滚动 {amount}px。"
            else:  # up
                await self._page.evaluate(f"window.scrollBy(0, -{amount})")
                result_msg = f"已向上滚动 {amount}px。"
            
            # 等待动态内容加载（如懒加载图片、无限滚动列表）
            await asyncio.sleep(0.8)
            logger.info("Browser scroll direction=%s amount=%d", direction, amount)
            return result_msg + " 请调用 browser_get_state 查看滚动后的页面内容。"
        except Exception as e:
            logger.error("Scroll failed: %s", e)
            return f"滚动失败：{e}"

    async def close(self) -> None:
        """关闭浏览器实例。"""
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
            if self._chrome_stderr_file:
                try:
                    self._chrome_stderr_file.close()
                except Exception:
                    pass
                self._chrome_stderr_file = None
                    
            logger.info("Playwright 浏览器已关闭。")

