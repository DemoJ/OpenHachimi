"""Browser 管理服务，用于提供 Playwright 支持和可访问性树截取。"""

import asyncio
import logging
import random
import os
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
        self._lock = asyncio.Lock()
        
        # 存储当前页面的可交互元素映射表：id -> locator
        # 这样 LLM 只需要返回一个数字 ID 就能点击
        self._element_mapping: dict[int, str] = {}

    def _find_chrome_executable(self) -> str:
        """寻找系统中真实的 Chrome/Edge 可执行文件路径"""
        config_path = getattr(self.config, 'browser_channel', '')
        if config_path and os.path.isabs(config_path) and os.path.exists(config_path):
            return config_path
            
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
            paths = ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "microsoft-edge"]
            for p in paths:
                cmd = shutil.which(p)
                if cmd:
                    return cmd
                    
        raise RuntimeError("无法找到系统中安装的 Chrome 或 Edge 浏览器。请在 config.yaml 中配置 browser_channel 指定绝对路径。")


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
                    port = 9222
                    
                    args = [
                        chrome_path,
                        f"--remote-debugging-port={port}",
                        f"--user-data-dir={user_data_dir}",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ]
                    if headless:
                        args.extend(["--headless=new"])
                        
                    logger.info("以 CDP 接管模式启动原生浏览器: %s", chrome_path)
                    self._chrome_process = subprocess.Popen(
                        args,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    
                    # 等待调试端口就绪
                    max_retries = 30
                    port_ready = False
                    for _ in range(max_retries):
                        try:
                            response = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                            if response.getcode() == 200:
                                port_ready = True
                                break
                        except (urllib.error.URLError, ConnectionError):
                            pass
                        await asyncio.sleep(0.5)
                        
                    if not port_ready:
                        raise RuntimeError(f"等待浏览器 CDP 端口 {port} 就绪超时！可能已被其他程序占用，请尝试关闭所有 Chrome 窗口。")
                        
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
        """获取当前页面的简化可访问性树（包含元素 ID），供大模型阅读。"""
        await self._update_active_page()
        
        if not self._page or self._page.is_closed():
            return "当前没有打开的页面，请先使用 browser_navigate 导航到网页。"

        logger.info("获取当前页面状态（Accessibility Tree）...")
        
        # 重置映射表
        self._element_mapping = {}
        
        # 获取 ariaSnapshot
        # 由于 page.accessibility.snapshot() 被弃用且复杂，我们使用 Playwright 的 locator 获取常见的交互元素
        try:
            # 对于现代单页应用(SPA)如推特，networkidle几乎总是因为后台心跳或长轮询而超时
            # 所以我们用固定的短暂等待替代，或者等待 domcontentloaded
            await asyncio.sleep(0.5)
        except Exception:
            pass
            
        try:
            # 我们通过执行一段 JS 来获取页面上的交互元素并分配临时 ID
            # 这种方法比分析复杂的 ariaSnapshot 更直接稳定
            script = """
            () => {
                let idCounter = 1;
                const elements = [];
                const interactiveNodes = []; // 仅记录交互节点，用于过滤子文本
                
                // 遍历所有节点，以防漏掉隐藏在深处或没有标准语义的魔改节点
                const nodes = document.querySelectorAll('*');
                const winHeight = window.innerHeight;
                const winWidth = window.innerWidth;
                
                for (const node of nodes) {
                    // 1. 过滤特殊与无用标签
                    const tagName = node.tagName.toLowerCase();
                    if (['script', 'style', 'noscript', 'meta', 'link', 'head'].includes(tagName)) continue;
                    
                    // 2. 视口与尺寸严格过滤
                    const rect = node.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    // 剔除屏幕外元素，强制大模型只能看到视口内的内容
                    if (rect.bottom < 0 || rect.top > winHeight || rect.right < 0 || rect.left > winWidth) continue;
                    
                    // 剔除完全透明或被隐藏的元素
                    const style = window.getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                    
                    // 3. 通用交互性检测 (包含标准属性与鼠标手势探测)
                    const isEditable = tagName === 'input' || tagName === 'textarea' || 
                                       node.isContentEditable || 
                                       node.getAttribute('role') === 'textbox' || 
                                       node.getAttribute('role') === 'combobox';
                                       
                    let isInteractive = isEditable || tagName === 'a' || tagName === 'button' || tagName === 'select' ||
                                        node.getAttribute('role') === 'button' || node.getAttribute('role') === 'link' ||
                                        node.getAttribute('role') === 'menuitem' || node.getAttribute('role') === 'option' ||
                                        (node.hasAttribute('tabindex') && node.getAttribute('tabindex') !== '-1') ||
                                        style.cursor === 'pointer' || style.cursor === 'text';
                                        
                    // 4. 物理遮挡剔除 (Occlusion Culling)
                    if (isInteractive) {
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top + rect.height / 2;
                        if (centerX >= 0 && centerX <= winWidth && centerY >= 0 && centerY <= winHeight) {
                            const topEl = document.elementFromPoint(centerX, centerY);
                            // 如果顶级元素不是自己，也不是包含关系
                            if (topEl && topEl !== node && !node.contains(topEl) && !topEl.contains(node)) {
                                // 寻找最近公共祖先 (防误杀：如富文本编辑器的 placeholder 兄弟节点覆盖了 input)
                                let p1 = node;
                                let common = null;
                                let depth = 0;
                                while(p1 && depth < 5) {
                                    if (p1.contains(topEl)) {
                                        common = p1;
                                        break;
                                    }
                                    p1 = p1.parentElement;
                                    depth++;
                                }
                                // 如果 5 层之内找不到公共祖先，说明是被毫不相干的浮层遮挡了
                                if (!common) {
                                    isInteractive = false; // 降级为非交互节点
                                }
                            }
                        }
                    }
                    
                    // 5. 文本智能提取
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
                        // 非交互元素仅提取它直属的文本节点，杜绝因为 innerText 导致的父子文本“俄罗斯套娃”重复
                        let directText = '';
                        for (let child of node.childNodes) {
                            if (child.nodeType === 3) directText += child.textContent;
                        }
                        directText = directText.trim();
                        if (directText) {
                            text = node.getAttribute('aria-label') || node.getAttribute('alt') || directText;
                        } else {
                            continue; // 彻底没有任何自身信息的无用节点
                        }
                    }
                    
                    text = text.replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim();
                    if (text.length > 100) text = text.substring(0, 100) + '...';
                    
                    if (!text && !isInteractive) continue;
                    
                    // 6. 祖先去重：如果当前纯文本节点的某个交互父级已经被提取过了，就不再作为独立节点输出，以防干扰模型
                    if (!isInteractive) {
                        if (interactiveNodes.some(parent => parent.contains(node))) {
                            continue;
                        }
                    }
                    
                    const role = node.getAttribute('role') || tagName;
                    const elData = {
                        id: idCounter++,
                        tag: tagName,
                        role: role,
                        text: text,
                        type: node.type || undefined,
                        isInteractive: isInteractive
                    };
                    
                    node.setAttribute('data-agent-id', elData.id);
                    elements.push(elData);
                    
                    if (isInteractive) {
                        interactiveNodes.push(node);
                    }
                }
                
                return {
                    url: document.location.href,
                    title: document.title,
                    elements: elements
                };
            }
            """
            
            result = await self._page.evaluate(script)
            
            output = [f"URL: {result['url']}"]
            output.append(f"Title: {result['title']}")
            output.append("-" * 40)
            output.append("Interactive Elements (带有 [*] 标记的为可交互元素):")
            
            for el in result["elements"]:
                # 保存映射表用于后续操作：通过 data-agent-id 定位
                self._element_mapping[el['id']] = f"[data-agent-id='{el['id']}']"
                
                type_str = f" [type: {el['type']}]" if el.get('type') else ""
                interactive_mark = " [*]" if el.get('isInteractive') else ""
                output.append(f"[{el['id']}]{interactive_mark} {el['role'].upper()}{type_str}: {el['text']}")
                
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
                    
            logger.info("Playwright 浏览器已关闭。")

