"""Main browser manager facade."""

import asyncio
import logging
import random
import sys

from playwright.async_api import Browser, BrowserContext, Page

from openhachimi_agent.core.config import AppConfig
from .lifecycle import BrowserLifecycleMixin
from .dom_scripts import DETECT_HUMAN_VERIFICATION_SCRIPT, GET_STATE_SCRIPT
from .utils import _human_verification_message

logger = logging.getLogger(__name__)


class BrowserManager(BrowserLifecycleMixin):
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

    async def _detect_human_verification(self) -> str | None:
        """Detect common CAPTCHA/challenge pages and ask for human takeover."""
        if not self._page or self._page.is_closed():
            return None

        try:
            signal = await self._page.evaluate(DETECT_HUMAN_VERIFICATION_SCRIPT)
        except Exception as exc:
            logger.debug("human verification detection failed: %s", exc)
            return None

        if signal:
            logger.warning("human verification detected url=%s reason=%s", self._page.url, signal)
            return str(signal)
        return None

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
            if reason := await self._detect_human_verification():
                return _human_verification_message(page.url, reason)
            return f"当前已在 {page.url}，无需重复导航。请直接使用 browser_get_state 获取页面内容。"
            
        logger.info("Browser navigating to: %s", url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(0.8, 1.6))
            if reason := await self._detect_human_verification():
                return _human_verification_message(page.url, reason)
            return f"成功导航到：{page.url}。请使用 browser_get_state 获取页面内容。"
        except Exception as e:
            logger.error("Navigation failed: %s", e)
            return f"导航失败：{e}"

    async def get_state(self) -> str:
        """获取当前页面的完整可访问性树（包含元素 ID），供大模型阅读。"""
        await self._update_active_page()
        
        if not self._page or self._page.is_closed():
            return "当前没有打开的页面，请先使用 browser_navigate 导航到网页。"

        logger.info("获取当前页面状态（Accessibility Tree）...")
        if reason := await self._detect_human_verification():
            return _human_verification_message(self._page.url, reason)
        
        # 重置映射表
        self._element_mapping = {}
        
        try:
            await asyncio.sleep(0.5)
        except Exception:
            pass
            
        # 单次返回全页元素的上限，防止超大页面撑爆模型上下文
        MAX_ELEMENTS = 500

        try:
            result = await self._page.evaluate(GET_STATE_SCRIPT, MAX_ELEMENTS)
            
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
            await asyncio.sleep(random.uniform(0.2, 0.6))
            try:
                await self._page.locator(selector).first.click(timeout=5000, delay=random.randint(10, 50))
            except Exception:
                await self._page.locator(selector).first.click(timeout=3000, force=True, delay=random.randint(10, 50))
            try:
                await asyncio.sleep(1.0)
                await self._update_active_page()
            except Exception:
                pass
            if reason := await self._detect_human_verification():
                return _human_verification_message(self._page.url, reason)
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
            try:
                await locator.click(timeout=5000)
            except Exception:
                await locator.click(timeout=3000, force=True)
            await asyncio.sleep(random.uniform(0.1, 0.4))
            
            is_mac = sys.platform == "darwin"
            modifier = "Meta" if is_mac else "Control"
            
            try:
                await self._page.keyboard.press(f"{modifier}+A")
                await self._page.keyboard.press("Backspace")
                await asyncio.sleep(0.1)
            except Exception:
                pass
                
            await locator.press_sequentially(text, delay=random.randint(10, 30), timeout=10000)
            if reason := await self._detect_human_verification():
                return _human_verification_message(self._page.url, reason)
            return f"成功在元素 [{element_id}] 输入文本。"
        except Exception as e:
            return f"输入文本失败：{e}"

    async def scroll(self, direction: str, amount: int = 600) -> str:
        """滚动页面。"""
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
            
            await asyncio.sleep(0.8)
            logger.info("Browser scroll direction=%s amount=%d", direction, amount)
            if reason := await self._detect_human_verification():
                return _human_verification_message(self._page.url, reason)
            return result_msg + " 请调用 browser_get_state 查看滚动后的页面内容。"
        except Exception as e:
            logger.error("Scroll failed: %s", e)
            return f"滚动失败：{e}"
