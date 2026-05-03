"""Browser 管理服务，用于提供 Playwright 支持和可访问性树截取。"""

import asyncio
import logging
import random
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
        self._lock = asyncio.Lock()
        
        # 存储当前页面的可交互元素映射表：id -> locator
        # 这样 LLM 只需要返回一个数字 ID 就能点击
        self._element_mapping: dict[int, str] = {}

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

            if not self._context:
                headless = self.config.browser_headless
                channel = self.config.browser_channel
                user_data_dir = self.config.base_dir / ".browser_data"

                try:
                    # 使用极其普通的现代浏览器 User-Agent
                    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    kwargs = {
                        "headless": headless,
                        "viewport": {"width": 1280, "height": 800},
                        "device_scale_factor": 1,
                        "user_agent": ua,
                        "args": ["--disable-blink-features=AutomationControlled"]
                    }
                    if channel:
                        kwargs["channel"] = channel
                    
                    if not headless:
                        # 非无头模式下使用持久化上下文，保留登录信息
                        self._context = await self._playwright.chromium.launch_persistent_context(
                            user_data_dir=user_data_dir,
                            **kwargs
                        )
                    else:
                        if not self._browser:
                            browser_kwargs = {"headless": headless}
                            if channel:
                                browser_kwargs["channel"] = channel
                            self._browser = await self._playwright.chromium.launch(**browser_kwargs)
                            
                        self._context = await self._browser.new_context(
                            viewport={"width": 1280, "height": 800},
                            device_scale_factor=1,
                            user_agent=ua
                        )
                        
                    # 应用 playwright-stealth 插件，隐藏 WebDriver 痕迹，模拟真实浏览器
                    await Stealth().apply_stealth_async(self._context)

                except PlaywrightError as e:
                    if "Executable doesn't exist" in str(e):
                        raise RuntimeError("未找到浏览器内核，请先执行 `.venv/Scripts/playwright.exe install chromium` 安装，或在 config.yaml 中配置 browser_channel 使用本地浏览器。") from e
                    raise

            pages = self._context.pages
            if pages:
                self._page = pages[-1]
            else:
                self._page = await self._context.new_page()
            logger.info("Playwright 浏览器已启动并打开新页面。")
            return self._page

    async def _update_active_page(self):
        """如果打开了新标签页，自动切换到最新的标签页"""
        if self._context and self._context.pages:
            newest_page = self._context.pages[-1]
            if self._page != newest_page:
                logger.info("检测到新标签页，自动切换当前页面。")
                self._page = newest_page
                try:
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
            # 等待网络空闲
            await self._page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass # 忽略超时
            
        try:
            # 我们通过执行一段 JS 来获取页面上的交互元素并分配临时 ID
            # 这种方法比分析复杂的 ariaSnapshot 更直接稳定
            script = """
            () => {
                let idCounter = 1;
                const elements = [];
                
                // 常见的交互元素和文本内容
                const selectors = 'a, button, input, textarea, select, [role="button"], [role="link"], h1, h2, h3, p, span';
                const nodes = document.querySelectorAll(selectors);
                
                for (const node of nodes) {
                    // 检查元素是否可见
                    const rect = node.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0 || window.getComputedStyle(node).visibility === 'hidden') {
                        continue;
                    }
                    
                    const tagName = node.tagName.toLowerCase();
                    const text = (node.innerText || node.value || node.getAttribute('aria-label') || node.getAttribute('alt') || '').trim().substring(0, 50);
                    
                    if (!text && tagName !== 'input' && tagName !== 'textarea') {
                        continue;
                    }
                    
                    const role = node.getAttribute('role') || tagName;
                    const elData = {
                        id: idCounter++,
                        tag: tagName,
                        role: role,
                        text: text.replace(/\\n/g, ' '),
                        type: node.type || undefined
                    };
                    
                    // 给 DOM 元素打上临时标记方便后续 Playwright locator 点击
                    node.setAttribute('data-agent-id', elData.id);
                    elements.push(elData);
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
            output.append("Interactive Elements:")
            
            for el in result["elements"]:
                # 保存映射表用于后续操作：通过 data-agent-id 定位
                self._element_mapping[el['id']] = f"[data-agent-id='{el['id']}']"
                
                type_str = f" [type: {el['type']}]" if el.get('type') else ""
                output.append(f"[{el['id']}] {el['role'].upper()}{type_str}: {el['text']}")
                
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
            # 尝试点击，如果被遮挡，可以使用 force=True。增加随机按压延迟模拟人类
            await self._page.locator(selector).first.click(timeout=5000, delay=random.randint(10, 50))
            # 等待可能发生的页面跳转或加载
            try:
                # 等待一会儿让新标签页有机会打开
                await asyncio.sleep(0.5)
                await self._update_active_page()
                await self._page.wait_for_load_state("networkidle", timeout=3000)
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
            await locator.click(timeout=5000)
            await asyncio.sleep(random.uniform(0.1, 0.4))
            # 清空已有内容
            await locator.fill("")
            # 逐字输入文本，带有随机延迟
            await locator.press_sequentially(text, delay=random.randint(50, 150), timeout=10000)
            return f"成功在元素 [{element_id}] 输入文本。"
        except Exception as e:
            return f"输入文本失败：{e}"

    async def close(self) -> None:
        """关闭浏览器实例。"""
        async with self._lock:
            if self._page:
                await self._page.close()
                self._page = None
            if self._context:
                await self._context.close()
                self._context = None
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            logger.info("Playwright 浏览器已关闭。")

