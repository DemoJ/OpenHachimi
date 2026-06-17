"""Main browser manager facade."""

import asyncio
import json
import logging
import random
import sys
import time
from functools import wraps

from playwright.async_api import Browser, BrowserContext, Page, Error as PlaywrightError

from openhachimi_agent.core.config import AppConfig
from .lifecycle import BrowserLifecycleMixin
from .dom_scripts import DETECT_HUMAN_VERIFICATION_SCRIPT, EXTRACT_CONTENT_SCRIPT, GET_STATE_SCRIPT, MUTATION_OBSERVER_SCRIPT
from .captcha_patterns import CAPTCHA_PATTERNS
from .utils import _human_verification_message

logger = logging.getLogger(__name__)


def auto_heal_retry(max_retries=3, base_delay=1.0):
    """
    自动恢复重试装饰器（仅对 Playwright 断连/页面崩溃类瞬时错误重试）。

    策略：
    - 只对被判定为「断连 / 会话已关闭 / 目标已消失」的错误重试，并在重试前调用
      ``_ensure_browser`` 重新获取浏览器和页面。
    - 其它异常（例如 KeyError、AttributeError、业务校验错误）一律视为代码 bug 或
      真实业务错误，不重试、立即以 ``BROWSER_OP_FAILED:`` 前缀返回，避免对真正的
      bug 反复重试浪费时间和把日志噪声放大。

    设计契约：被装饰的方法面向 LLM 工具，错误以中文字符串返回（与 BrowserManager
    内部其它方法保持一致）。重试耗尽或非瞬时错误时的失败字符串以
    ``BROWSER_OP_FAILED:`` 开头，方便监控和自动化通过前缀程序化识别失败，而不必
    依赖整句中文匹配。
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            last_err = None
            for attempt in range(max_retries):
                try:
                    return await func(self, *args, **kwargs)
                except Exception as e:
                    last_err = e
                    err_str = str(e).lower()
                    # 检查是否为连接断开或目标关闭类型的错误
                    is_disconnect = (
                        isinstance(e, PlaywrightError) and
                        any(kw in err_str for kw in ["closed", "disconnected", "target", "protocol error", "session"])
                    ) or "not open" in err_str

                    # 非瞬时错误：立即失败，不重试。避免对代码 bug（KeyError 等）
                    # 或业务错误反复重试，把单次失败放大成 N 倍延迟与日志噪声。
                    if not is_disconnect:
                        logger.error(
                            "Non-transient error in '%s', not retrying: %s",
                            func.__name__, e, exc_info=True,
                        )
                        return f"BROWSER_OP_FAILED: {func.__name__} 操作失败：{e}"

                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                        logger.warning(
                            "Browser disconnected during '%s'. Attempting to heal and retry in %.1fs...",
                            func.__name__, delay, exc_info=True,
                        )
                        await asyncio.sleep(delay)
                        # 强制重置当前页面，让 _ensure_browser 重新获取或创建
                        if getattr(self, "_page", None):
                            self._page = None
                        try:
                            await self._ensure_browser()
                        except Exception as heal_err:
                            logger.error("Auto-heal failed: %s", heal_err, exc_info=True)
                    else:
                        break
            # 重试耗尽：保留 traceback 方便排查，向调用方返回带前缀的失败字符串。
            logger.error(
                "'%s' failed after %d retries. Last error: %s",
                func.__name__, max_retries, last_err, exc_info=last_err,
            )
            return f"BROWSER_OP_FAILED: {func.__name__} 操作最终失败：{last_err}"
        return wrapper
    return decorator


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
        self._chrome_cdp_port: int | None = None  # 记录当前 CDP 端口，用于复用检测
        self._lock = asyncio.Lock()
        # 操作级串行锁：把面向外部的 navigate/get_state/click/type_text/scroll 等串行化，
        # 防止跨会话并发操作把共享的 _page / _element_mapping 撕裂
        # （例如会话 A 的 click 撞上会话 B 的 get_state 重建 mapping → 点错元素）。
        # _lock 仅用于浏览器生命周期（启动/关闭），不可与 _op_lock 互换：
        # 操作内部会调用 _ensure_browser，后者会再次获取 _lock，
        # 因此必须保证 _op_lock → _lock 的获取顺序，避免死锁。
        self._op_lock = asyncio.Lock()
        
        # 存储当前页面的可交互元素映射表：id -> locator
        # 这样 LLM 只需要返回一个数字 ID 就能点击
        self._element_mapping: dict[int, str] = {}
        
        self._captcha_detected_reason: str | None = None
        self._captcha_setup_context_id = None
        
        self._last_activity_time: float = time.time()
        self._idle_monitor_task: asyncio.Task | None = None

    def _record_activity(self):
        """记录浏览器活跃时间戳"""
        self._last_activity_time = time.time()

    async def _idle_monitor_loop(self):
        """后台轮询，检测浏览器空闲是否超时"""
        timeout = self.config.browser_idle_timeout
        if timeout <= 0:
            return
            
        while True:
            await asyncio.sleep(30)
            
            # 只有在浏览器存活时才检测
            if self._browser and getattr(self._browser, "is_connected", lambda: True)():
                idle_time = time.time() - self._last_activity_time
                if idle_time > timeout:
                    logger.info("浏览器已空闲超过 %d 秒，自动触发 close() 释放资源。", timeout)
                    try:
                        await self.close()
                    except Exception as e:
                        logger.error("自动关闭空闲浏览器失败: %s", e)

    async def _handle_captcha_detected(self, reason: str):
        if not self._captcha_detected_reason:
            self._captcha_detected_reason = reason
            logger.warning("MutationObserver detected captcha: %s", reason)

    async def _ensure_browser(self):
        page = await super()._ensure_browser()
        
        self._record_activity()
        
        # 懒加载启动监控协程
        if not self._idle_monitor_task or self._idle_monitor_task.done():
            if self.config.browser_idle_timeout > 0:
                self._idle_monitor_task = asyncio.create_task(self._idle_monitor_loop())
        
        # 避免在同一个 context 重复注入
        current_context_id = id(self._context)
        if self._captcha_setup_context_id != current_context_id:
            try:
                # 尝试清除之前的状态
                self._captcha_detected_reason = None
                await self._context.expose_function("onCaptchaDetected", self._handle_captcha_detected)
                await self._context.add_init_script(f"({MUTATION_OBSERVER_SCRIPT})({json.dumps(CAPTCHA_PATTERNS)})")
                self._captcha_setup_context_id = current_context_id
            except Exception as e:
                logger.debug("Failed to setup captcha observer on context: %s", e)
                
            # 对当前页面立即执行一次
            try:
                await page.evaluate(MUTATION_OBSERVER_SCRIPT, CAPTCHA_PATTERNS)
            except Exception:
                pass
                
        return page

    async def _detect_human_verification(self) -> str | None:
        """Detect common CAPTCHA/challenge pages and ask for human takeover."""
        if self._captcha_detected_reason:
            return self._captcha_detected_reason
            
        if not self._page or self._page.is_closed():
            return None

        try:
            signal = await self._page.evaluate(DETECT_HUMAN_VERIFICATION_SCRIPT, CAPTCHA_PATTERNS)
        except Exception as exc:
            logger.debug("human verification detection failed: %s", exc)
            return None

        if signal:
            self._captcha_detected_reason = str(signal)
            logger.warning("human verification detected url=%s reason=%s", self._page.url, signal)
            return str(signal)
        return None

    def _get_valid_pages(self):
        """获取当前上下文中除了内置页面外所有的有效标签页。"""
        if not self._context or not self._context.pages:
            return []
        return [p for p in self._context.pages if not (".top-chrome" in p.url or "chrome-extension://" in p.url)]

    async def _update_active_page(self):
        """确保当前有一个激活的标签页置于前台显示。"""
        valid_pages = self._get_valid_pages()
        if not valid_pages:
            return
            
        # 仅在当前页面丢失/关闭时，才自动回退到最新标签页
        if not self._page or self._page.is_closed() or self._page not in valid_pages:
            self._page = valid_pages[-1]
            logger.info("当前标签页已失效或未绑定，自动回退到标签页: %s", self._page.url)
            
        try:
            # 每次执行动作前，强制把 Agent 正在操作的标签页切到最前面
            await self._page.bring_to_front()
        except Exception:
            pass

    async def list_tabs(self) -> str:
        """获取并列出当前打开的所有标签页。"""
        async with self._op_lock:
            self._record_activity()
            valid_pages = self._get_valid_pages()
            if not valid_pages:
                return "当前没有打开任何标签页。"

            lines = ["[打开的标签页列表]"]
            for i, p in enumerate(valid_pages):
                try:
                    title = await p.title()
                except Exception:
                    title = "Unknown Title"
                active_mark = " (当前活动)" if p == self._page else ""
                lines.append(f"[{i}] {title} - {p.url}{active_mark}")

            return "\n".join(lines)

    async def new_tab(self, url: str = None) -> str:
        """新建一个标签页并将其激活。"""
        async with self._op_lock:
            self._record_activity()
            await self._ensure_browser()

            try:
                new_page = await self._context.new_page()
                self._page = new_page

                # 手动注入一次 observer 以防 context hook 还没生效
                try:
                    await new_page.evaluate(MUTATION_OBSERVER_SCRIPT, CAPTCHA_PATTERNS)
                except Exception:
                    pass

                if url:
                    if not url.startswith("http"):
                        url = "https://" + url
                    self._captcha_detected_reason = None
                    await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if reason := await self._detect_human_verification():
                        return _human_verification_message(new_page.url, reason)
                    return f"已新建标签页并成功导航到：{new_page.url}"

                return "已新建空白标签页并激活。"
            except Exception as e:
                logger.error("Failed to create new tab: %s", e)
                return f"新建标签页失败：{e}"

    @auto_heal_retry()
    async def switch_tab(self, tab_index: int) -> str:
        """切换到指定索引的标签页。"""
        async with self._op_lock:
            self._record_activity()
            valid_pages = self._get_valid_pages()
            if not valid_pages:
                return "当前没有打开任何标签页。"

            if tab_index < 0 or tab_index >= len(valid_pages):
                return f"无效的标签页索引 {tab_index}。当前有效索引范围: 0 到 {len(valid_pages) - 1}。"

            self._page = valid_pages[tab_index]
            await self._update_active_page()

            try:
                title = await self._page.title()
            except Exception:
                title = "Unknown Title"

            if reason := await self._detect_human_verification():
                return _human_verification_message(self._page.url, reason)

            return f"已成功切换到标签页 [{tab_index}] {title} - {self._page.url}"

    async def close_tab(self, tab_index: int = None) -> str:
        """关闭指定索引的标签页。如果不传则关闭当前活动的标签页。"""
        async with self._op_lock:
            self._record_activity()
            valid_pages = self._get_valid_pages()
            if not valid_pages:
                return "当前没有打开任何标签页。"

            if tab_index is not None:
                if tab_index < 0 or tab_index >= len(valid_pages):
                    return f"无效的标签页索引 {tab_index}。当前有效索引范围: 0 到 {len(valid_pages) - 1}。"
                target_page = valid_pages[tab_index]
            else:
                target_page = self._page

            try:
                await target_page.close()
                # 刷新页面列表
                remaining_pages = self._get_valid_pages()
                if not remaining_pages:
                    self._page = None
                    return "标签页已关闭。目前所有标签页都已关闭，请新建标签页或重新导航。"

                # 如果关掉的是当前激活的页面，自动更新到最新的标签页
                if target_page == self._page:
                    self._page = remaining_pages[-1]
                    await self._update_active_page()
                    return f"标签页已关闭，自动切换到剩余的标签页：{self._page.url}。"

                return "标签页已关闭。"
            except Exception as e:
                logger.error("Failed to close tab: %s", e)
                return f"关闭标签页失败：{e}"

    async def wait_for_load_state(self, state: str = "load", selector: str = None, function: str = None, timeout: int = 15000) -> bool:
        """
        等待页面加载状态。
        支持的 state 策略：'load', 'domcontentloaded', 'networkidle', 'selector', 'function'
        """
        if not self._page or self._page.is_closed():
            return False
        try:
            if state in ["load", "domcontentloaded", "networkidle"]:
                await self._page.wait_for_load_state(state, timeout=timeout)
            elif state == "selector" and selector:
                await self._page.wait_for_selector(selector, state="visible", timeout=timeout)
            elif state == "function" and function:
                await self._page.wait_for_function(function, timeout=timeout)
            return True
        except Exception as e:
            logger.debug("wait_for_load_state '%s' timeout or failed: %s", state, e)
            return False

    async def navigate(self, url: str) -> str:
        """导航到指定网页。"""
        async with self._op_lock:
            self._record_activity()
            if not url.startswith("http"):
                url = "https://" + url

            # 导航前重置检测状态
            self._captcha_detected_reason = None

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
                await self.wait_for_load_state("networkidle", timeout=5000)
                if reason := await self._detect_human_verification():
                    return _human_verification_message(page.url, reason)
                return f"成功导航到：{page.url}。请使用 browser_get_state 获取页面内容。"
            except Exception as e:
                logger.error("Navigation failed: %s", e)
                return f"导航失败：{e}"

    async def get_state(self) -> str:
        """获取当前页面的完整可访问性树（包含元素 ID），供大模型阅读。"""
        async with self._op_lock:
            self._record_activity()
            await self._update_active_page()

            if not self._page or self._page.is_closed():
                return "当前没有打开的页面，请先使用 browser_navigate 导航到网页。"

            logger.info("获取当前页面状态（Accessibility Tree）...")
            if reason := await self._detect_human_verification():
                return _human_verification_message(self._page.url, reason)

            # 先在局部 dict 上构建新的映射，构建完成后再原子替换 self._element_mapping，
            # 避免任何中间态被并发的 click/type_text 读到。
            new_mapping: dict[int, str] = {}

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
                    new_mapping[el['id']] = f"[data-agent-id='{el['id']}']"

                    type_str     = f" [type:{el['type']}]" if el.get('type') else ""
                    interact_mark = " [*]" if el.get('isInteractive') else ""
                    pos = el.get('position', 'viewport')
                    pos_mark = "" if pos == 'viewport' else (" [↑]" if pos == 'above' else " [↓]")
                    output.append(f"[{el['id']}]{interact_mark}{pos_mark} {el['role'].upper()}{type_str}: {el['text']}")

                # 全部构建完成后才发布到 self._element_mapping，确保读到的要么是旧的、要么是新的，没有中间态。
                self._element_mapping = new_mapping
                return "\n".join(output)

            except Exception as e:
                logger.error("Failed to get state: %s", e)
                return f"获取页面状态失败：{e}"

    async def extract_content(self, max_chars: int = 60000, include_links: bool = True) -> str:
        """提取当前页面正文、metadata、标题和链接，供研究任务读取。"""
        async with self._op_lock:
            self._record_activity()
            await self._update_active_page()

            if not self._page or self._page.is_closed():
                return "当前没有打开的页面，请先使用 browser_navigate 导航到网页。"

            logger.info("提取当前页面正文内容...")
            if reason := await self._detect_human_verification():
                return _human_verification_message(self._page.url, reason)

            try:
                result = await self._page.evaluate(
                    EXTRACT_CONTENT_SCRIPT,
                    {"maxChars": max_chars, "includeLinks": include_links, "maxLinks": 80},
                )
                metadata = result.get("metadata") or {}
                content = result.get("content") or {}
                page_state = result.get("pageState") or {}

                output = [
                    f"URL: {result.get('url', '')}",
                    f"Title: {result.get('title', '')}",
                    f"ReadyState: {result.get('readyState', '')}",
                    f"Lang: {result.get('lang', '') or 'unknown'}",
                    f"Source selector: {content.get('sourceSelector', 'unknown')}",
                    f"Text length: {content.get('textLength', 0)}",
                    f"Truncated: {content.get('truncated', False)}",
                    f"Scroll: y={page_state.get('scrollY', 0)} height={page_state.get('scrollHeight', 0)} viewport={page_state.get('clientHeight', 0)}",
                    "-" * 40,
                    "Metadata:",
                ]
                for key in ("description", "canonical", "author", "publishedTime", "modifiedTime", "ogTitle", "ogDescription", "ogSiteName"):
                    value = metadata.get(key)
                    if value:
                        output.append(f"- {key}: {value}")

                headings = result.get("headings") or []
                if headings:
                    output.append("")
                    output.append("Headings:")
                    for item in headings[:40]:
                        output.append(f"- {item.get('level', '').upper()}: {item.get('text', '')}")

                links = result.get("links") or []
                if include_links and links:
                    output.append("")
                    output.append("Links:")
                    for index, link in enumerate(links[:80], start=1):
                        text = link.get("text") or "（无文本）"
                        href = link.get("href") or ""
                        external = " external" if link.get("isExternal") else ""
                        output.append(f"{index}. {text} - {href}{external}")

                output.append("")
                output.append("Content:")
                output.append("-" * 40)
                output.append(content.get("text") or "（未提取到正文文本）")
                return "\n".join(output)
            except Exception as e:
                logger.error("Failed to extract page content: %s", e)
                return f"提取页面正文失败：{e}"

    async def click(self, element_id: int) -> str:
        """点击指定 ID 的元素。"""
        async with self._op_lock:
            self._record_activity()
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

    async def type_text(self, element_id: int, text: str, simulate_typing: bool = False) -> str:
        """在指定 ID 的输入框中输入文本。"""
        async with self._op_lock:
            self._record_activity()
            await self._update_active_page()

            if not self._page or self._page.is_closed():
                return "当前没有打开的页面。"

            if element_id not in self._element_mapping:
                return f"找不到 ID 为 {element_id} 的元素，请先调用 browser_get_state 刷新状态。"

            selector = self._element_mapping[element_id]
            logger.info("Browser typing text in element_id=%d selector=%s simulate_typing=%s", element_id, selector, simulate_typing)

            try:
                locator = self._page.locator(selector).first

                if not simulate_typing:
                    # 默认使用原子的 fill 操作，速度快且能稳定触发框架的 v-model/onChange，自带清空机制
                    await locator.fill(text, timeout=10000)
                else:
                    # 模拟逐字敲击（针对必须触发下拉联想建议的动态搜索框）
                    try:
                        await locator.click(timeout=5000)
                    except Exception:
                        await locator.click(timeout=3000, force=True)
                    await asyncio.sleep(random.uniform(0.1, 0.4))

                    # 先清空已有内容
                    try:
                        await locator.clear(timeout=3000)
                    except Exception:
                        # 极少情况下的回退逻辑
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

    @auto_heal_retry()
    async def scroll(self, direction: str, amount: int = 600, element_id: int | None = None) -> str:
        """滚动页面或局部容器。"""
        async with self._op_lock:
            self._record_activity()
            await self._update_active_page()

            if not self._page or self._page.is_closed():
                return "当前没有打开的页面。"

            direction = direction.strip().lower()
            if direction not in ("up", "down", "top", "bottom"):
                return f"不支持的滚动方向：{direction}，请使用 up / down / top / bottom。"

            try:
                if element_id is not None:
                    if element_id not in self._element_mapping:
                        return f"找不到 ID 为 {element_id} 的元素，无法进行局部滚动。请先调用 browser_get_state 刷新状态或直接进行全局滚动。"
                    selector = self._element_mapping[element_id]

                    scroll_script = """
                    ({ selector, direction, amount }) => {
                        const el = document.querySelector(selector);
                        if (!el) return false;

                        let container = el;
                        while (container && container !== document.body && container !== document.documentElement) {
                            if (container.scrollHeight > container.clientHeight) {
                                const style = window.getComputedStyle(container);
                                if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
                                    break;
                                }
                            }
                            container = container.parentElement;
                        }

                        if (!container || container === document.body || container === document.documentElement) {
                            container = window;
                        }

                        if (direction === 'top') {
                            container.scrollTo(0, 0);
                        } else if (direction === 'bottom') {
                            if (container === window) {
                                container.scrollTo(0, document.body.scrollHeight);
                            } else {
                                container.scrollTo(0, container.scrollHeight);
                            }
                        } else if (direction === 'down') {
                            container.scrollBy(0, amount);
                        } else {
                            container.scrollBy(0, -amount);
                        }
                        return true;
                    }
                    """

                    success = await self._page.evaluate(scroll_script, {
                        "selector": selector,
                        "direction": direction,
                        "amount": amount,
                    })
                    if not success:
                        return f"找不到该元素或局部滚动失败，可能是页面 DOM 发生了变化。"

                    direction_cn = {"top": "顶部", "bottom": "底部", "down": f"向下 {amount}px", "up": f"向上 {amount}px"}[direction]
                    result_msg = f"已针对元素 [{element_id}] 所在的局部容器滚动到 {direction_cn}。"
                else:
                    if direction == "top":
                        await self._page.evaluate("window.scrollTo(0, 0)")
                        result_msg = "已滚动到全局页面顶部。"
                    elif direction == "bottom":
                        await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        result_msg = "已滚动到全局页面底部。"
                    elif direction == "down":
                        await self._page.evaluate("(amount) => window.scrollBy(0, amount)", amount)
                        result_msg = f"已全局向下滚动 {amount}px。"
                    else:  # up
                        await self._page.evaluate("(amount) => window.scrollBy(0, -amount)", amount)
                        result_msg = f"已全局向上滚动 {amount}px。"

                await asyncio.sleep(0.8)
                logger.info("Browser scroll direction=%s amount=%d element_id=%s", direction, amount, element_id)
                if reason := await self._detect_human_verification():
                    return _human_verification_message(self._page.url, reason)
                return result_msg + " 请调用 browser_get_state 查看滚动后的页面内容。"
            except Exception as e:
                logger.error("Scroll failed: %s", e)
                return f"滚动失败：{e}"
