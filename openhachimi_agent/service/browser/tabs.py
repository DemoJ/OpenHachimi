"""标签页与导航 Mixin：list/new/switch/close tab + navigate + go_back/go_forward。

依赖宿主类提供（见 BrowserManager）：
- self._page / self._context / self._element_mappings / self._active_mapping / self._op_lock
- self._record_activity() / self._update_active_page() / self._ensure_browser()
- self._page_key() / self._get_valid_pages() / self._detect_human_verification()
- self._consume_dialog_report() / self.wait_for_load_state()
- auto_heal_retry / is_transient_disconnect（从 retry.py 导入）
"""

from __future__ import annotations

import asyncio
import logging

from .dom_scripts import MUTATION_OBSERVER_SCRIPT
from .captcha_patterns import CAPTCHA_PATTERNS
from .retry import auto_heal_retry, is_transient_disconnect
from .utils import _human_verification_message

logger = logging.getLogger(__name__)


class BrowserTabsMixin:
    """面向 LLM 工具的标签页与导航操作。"""

    @auto_heal_retry()
    async def list_tabs(self, session_id: str | None = None) -> str:
        """获取并列出当前打开的所有标签页。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
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
            finally:
                restore()

    @auto_heal_retry()
    async def new_tab(self, url: str = None, session_id: str | None = None) -> str:
        """新建一个标签页并将其激活。"""
        async with self._op_lock:
            self._record_activity()
            await self._ensure_browser()

            try:
                new_page = await self._context.new_page()
                self._page = new_page
                # 新页面不存在任何元素映射，活动映射立即切为空
                self._active_mapping = {}
                # 会话绑定：该会话后续操作自动路由到此页面
                self._bind_session_page(session_id, new_page)

                # 手动注入一次 observer 以防 context hook 还没生效
                try:
                    callback_name = getattr(self, "_captcha_callback_name", None)
                    patterns_with_cb = {**CAPTCHA_PATTERNS, "_cb": callback_name}
                    await new_page.evaluate(MUTATION_OBSERVER_SCRIPT, patterns_with_cb)
                except Exception:
                    pass

                if url:
                    url = self._normalize_url(url)
                    self._captcha_detected_reason = None
                    await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if reason := await self._detect_human_verification():
                        return _human_verification_message(new_page.url, reason)
                    return f"已新建标签页并成功导航到：{new_page.url}" + self._consume_dialog_report()

                return "已新建空白标签页并激活。"
            except Exception as e:
                if is_transient_disconnect(e):
                    raise  # 冒泡到 auto_heal_retry 触发自愈重试
                logger.error("Failed to create new tab: %s", e)
                return f"新建标签页失败：{e}" + self._consume_dialog_report()

    @auto_heal_retry()
    async def switch_tab(self, tab_index: int, session_id: str | None = None) -> str:
        """切换到指定索引的标签页。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                valid_pages = self._get_valid_pages()
                if not valid_pages:
                    return "当前没有打开任何标签页。"

                if tab_index < 0 or tab_index >= len(valid_pages):
                    return f"无效的标签页索引 {tab_index}。当前有效索引范围: 0 到 {len(valid_pages) - 1}。"

                self._page = valid_pages[tab_index]
                # 切换页面后必须切换到该页面自己的元素映射
                self._active_mapping = self._element_mappings.get(self._page_key(self._page), {})
                await self._update_active_page()
                # 会话绑定跟随切换
                self._bind_session_page(session_id, self._page)

                try:
                    title = await self._page.title()
                except Exception:
                    title = "Unknown Title"

                if reason := await self._detect_human_verification():
                    return _human_verification_message(self._page.url, reason)

                return f"已成功切换到标签页 [{tab_index}] {title} - {self._page.url}"
            finally:
                restore()

    @auto_heal_retry()
    async def close_tab(self, tab_index: int = None, session_id: str | None = None) -> str:
        """关闭指定索引的标签页。如果不传则关闭当前活动的标签页。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
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
                    # 无论关闭的是哪个页面，其映射都已随页面销毁失效
                    self._element_mappings.pop(self._page_key(target_page), None)
                    # 解除指向该页面的所有会话绑定
                    for sid, bound in list(self._session_pages.items()):
                        if bound is target_page:
                            self._session_pages.pop(sid, None)
                    # 刷新页面列表
                    remaining_pages = self._get_valid_pages()
                    if not remaining_pages:
                        self._page = None
                        self._active_mapping = {}
                        return "标签页已关闭。目前所有标签页都已关闭，请新建标签页或重新导航。"

                    # 如果关掉的是当前激活的页面，自动更新到最新的标签页
                    if target_page == self._page:
                        self._page = remaining_pages[-1]
                        self._active_mapping = self._element_mappings.get(self._page_key(self._page), {})
                        await self._update_active_page()
                        # 本会话绑定迁移到新页面
                        self._bind_session_page(session_id, self._page)
                        return f"标签页已关闭，自动切换到剩余的标签页：{self._page.url}。"

                    return "标签页已关闭。"
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise  # 冒泡到 auto_heal_retry 触发自愈重试
                    logger.error("Failed to close tab: %s", e)
                    return f"关闭标签页失败：{e}"
            finally:
                restore()

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

    def _normalize_url(self, url: str) -> str:
        """URL 规范化：补协议。本地地址（localhost/127.0.0.1/内网 IP）默认 http，其余 https。"""
        if url.startswith(("http://", "https://", "file://", "about:", "data:", "chrome://")):
            return url
        lowered = url.lower()
        local_prefixes = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "192.168.", "10.", "172.16.", "172.17.", "172.18.",
                          "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
                          "172.28.", "172.29.", "172.30.", "172.31.")
        if any(lowered.startswith(p) or f"://{p}" in lowered for p in local_prefixes):
            return "http://" + url
        return "https://" + url

    @auto_heal_retry()
    async def navigate(self, url: str, session_id: str | None = None) -> str:
        """导航到指定网页。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                url = self._normalize_url(url)

                # 导航前重置检测状态
                self._captcha_detected_reason = None

                page = await self._ensure_browser()
                await self._update_active_page()
                page = self._page
                # 导航成功后把会话绑定到当前页
                self._bind_session_page(session_id, self._page)

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
                    return f"成功导航到：{page.url}。请使用 browser_get_state 获取页面内容。" + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise  # 冒泡到 auto_heal_retry 触发自愈重试
                    logger.error("Navigation failed: %s", e)
                    return f"导航失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    @auto_heal_retry()
    async def go_back(self, session_id: str | None = None) -> str:
        """浏览器后退一步（返回上一页）。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                try:
                    self._captcha_detected_reason = None
                    went_back = await self._page.go_back(wait_until="domcontentloaded", timeout=15000)
                    if went_back is None:
                        return "已经处于历史记录起点，无法继续后退。"
                    # 后退后旧元素映射全部失效
                    self._active_mapping = {}
                    await self.wait_for_load_state("networkidle", timeout=5000)
                    if reason := await self._detect_human_verification():
                        return _human_verification_message(self._page.url, reason)
                    return f"已后退到：{self._page.url}。请使用 browser_get_state 获取页面内容。" + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise
                    logger.error("go_back failed: %s", e)
                    return f"后退失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    @auto_heal_retry()
    async def go_forward(self, session_id: str | None = None) -> str:
        """浏览器前进一步（撤销后退）。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                try:
                    self._captcha_detected_reason = None
                    went_forward = await self._page.go_forward(wait_until="domcontentloaded", timeout=15000)
                    if went_forward is None:
                        return "已经处于历史记录最新位置，无法继续前进。"
                    self._active_mapping = {}
                    await self.wait_for_load_state("networkidle", timeout=5000)
                    if reason := await self._detect_human_verification():
                        return _human_verification_message(self._page.url, reason)
                    return f"已前进到：{self._page.url}。请使用 browser_get_state 获取页面内容。" + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise
                    logger.error("go_forward failed: %s", e)
                    return f"前进失败：{e}" + self._consume_dialog_report()
            finally:
                restore()
