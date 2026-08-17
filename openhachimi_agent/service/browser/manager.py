"""BrowserManager 门面：生命周期编排 + context 加固 + 状态提取。

操作实现拆分至：
- ``tabs.py``        标签页管理 / navigate / go_back / go_forward
- ``interactions.py`` click / type / press_key / select_option / hover / scroll / wait_for
- ``lifecycle.py``   Chrome 进程与 CDP 连接生命周期
- ``retry.py``       断连自愈装饰器与判据

Mixin 依赖宿主属性（__init__ 全部初始化）：config / _playwright / _browser /
_context / _page / _chrome_process / _chrome_stderr_file / _chrome_cdp_port /
_lock / _op_lock / _element_mappings / _active_mapping / _captcha_detected_reason /
_captcha_setup_context_id / _context_hardening_id / _last_dialog /
_last_activity_time / _idle_monitor_task
"""

import asyncio
import json
import logging
import time

from playwright.async_api import Browser, BrowserContext, Page
from playwright_stealth import Stealth

from openhachimi_agent.core.config import AppConfig
from .lifecycle import BrowserLifecycleMixin
from .tabs import BrowserTabsMixin
from .interactions import BrowserInteractionsMixin
from .dom_scripts import DETECT_HUMAN_VERIFICATION_SCRIPT, EXTRACT_CONTENT_SCRIPT, GET_STATE_SCRIPT, MUTATION_OBSERVER_SCRIPT
from .captcha_patterns import CAPTCHA_PATTERNS
from .retry import auto_heal_retry, is_transient_disconnect
from .utils import _human_verification_message

logger = logging.getLogger(__name__)

__all__ = ["BrowserManager", "auto_heal_retry", "is_transient_disconnect"]


class BrowserManager(BrowserTabsMixin, BrowserInteractionsMixin, BrowserLifecycleMixin):
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
        # （例如会话 A 的 click 撞上会话 B 的 get_state 重建 mapping -> 点错元素）。
        # _lock 仅用于浏览器生命周期（启动/关闭），不可与 _op_lock 互换：
        # 操作内部会调用 _ensure_browser，后者会再次获取 _lock，
        # 因此必须保证 _op_lock -> _lock 的获取顺序，避免死锁。
        self._op_lock = asyncio.Lock()

        # 元素映射与页面强绑定：page 对象 -> {id -> selector}。
        # 页面关闭/切换/导航后旧映射即失效，防止跨页面残留 ID 命中错误元素。
        self._element_mappings: dict[int, dict[int, str]] = {}
        self._active_mapping: dict[int, str] = {}

        # 会话级标签页归属：session_id -> Page。
        # 多会话（多聊天/子 agent 委派）共享 browser_manager 时，各自操作路由到
        # 自己绑定的标签页，互不切页踩踏。session_id=None 保持全局单页行为。
        self._session_pages: dict[str, Page] = {}

        self._captcha_detected_reason: str | None = None
        self._captcha_setup_context_id = None
        # captcha observer 回调函数名（每个 context 随机生成，防指纹）
        self._captcha_callback_name: str | None = None
        # soft 级验证组件提示（不阻断操作，get_state 时附带告知 agent）
        self._captcha_soft_note: str | None = None
        # context 级 stealth/dialog 注入标记，避免重复注入
        self._context_hardening_id = None

        # 最近一次被自动 dismiss 的 JS dialog 信息（type, message），
        # 由 context 级 dialog 处理器写入，操作返回时向 agent 报告后清空。
        self._last_dialog: tuple[str, str] | None = None

        self._last_activity_time: float = time.time()
        self._idle_monitor_task: asyncio.Task | None = None

    # ---------- 活跃度监控 ----------

    # ---------- 会话级标签页路由 ----------

    def _resolve_session_page(self, session_id: str | None) -> Page | None:
        """把 session_id 路由到其绑定的页面；无绑定或已失效返回 None。

        session_id 为 None（历史调用方/未传）时不路由，沿用 self._page 全局行为，
        保证向后兼容。
        """
        if not session_id:
            return None
        page = self._session_pages.get(session_id)
        if page is None or page.is_closed():
            self._session_pages.pop(session_id, None)
            return None
        # 绑定页可能已不在有效列表（context 重建等），同样视为失效
        if page not in self._get_valid_pages():
            self._session_pages.pop(session_id, None)
            return None
        return page

    def _bind_session_page(self, session_id: str | None, page: Page) -> None:
        """记录会话 -> 页面绑定（page 为 None 时解绑）。"""
        if not session_id:
            return
        if page is None:
            self._session_pages.pop(session_id, None)
        else:
            self._session_pages[session_id] = page

    def _session_route(self, session_id: str | None):
        """操作前会话路由：绑定存在时把 self._page 临时指向该会话的页面。

        返回恢复函数；调用方在 finally 中调用以还原全局状态。
        这样 mixin 中的操作代码无需感知 session_id，保持单一 _page 流转。
        """
        bound = self._resolve_session_page(session_id)
        if bound is None or bound is self._page:
            return lambda: None
        original = self._page
        self._page = bound
        self._active_mapping = self._element_mappings.get(self._page_key(bound), {})

        def _restore():
            self._page = original
            self._active_mapping = (
                self._element_mappings.get(self._page_key(original), {})
                if original else {}
            )

        return _restore

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

    # ---------- 生命周期编排 ----------

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
                # 随机回调名：避免暴露 onCaptchaDetected 这类可指纹的固定全局函数，
                # observer 通过 patterns._cb 读取回调名（见 MUTATION_OBSERVER_SCRIPT）
                import secrets

                self._captcha_callback_name = f"_c{secrets.token_hex(6)}"
                await self._context.expose_function(self._captcha_callback_name, self._handle_captcha_detected)
                patterns_with_cb = {**CAPTCHA_PATTERNS, "_cb": self._captcha_callback_name}
                await self._context.add_init_script(f"({MUTATION_OBSERVER_SCRIPT})({json.dumps(patterns_with_cb)})")
                self._captcha_setup_context_id = current_context_id
            except Exception as e:
                logger.debug("Failed to setup captcha observer on context: %s", e)

            # 对当前页面立即执行一次
            try:
                patterns_with_cb = {**CAPTCHA_PATTERNS, "_cb": getattr(self, "_captcha_callback_name", None)}
                await page.evaluate(MUTATION_OBSERVER_SCRIPT, patterns_with_cb)
            except Exception:
                pass

        # context 级 stealth + dialog 处理（每个 context 只设置一次）
        await self._setup_context_hardening()

        return page

    async def _setup_context_hardening(self) -> None:
        """对当前 context 注入 stealth 与 dialog 处理器（幂等）。

        - stealth 用 add_init_script 覆盖所有后续页面（含 new_tab / window.open 弹出页），
          修复原实现只保护 _bind_active_page 绑定页的问题；
        - 接管外部浏览器（browser_connect_url）时跳过 stealth：外部实例通常是
          用户日常 Chrome，自身指纹真实完整，再叠加 stealth 反而制造特征矛盾
          （真实 UA + stealth 伪造的属性可能不一致），保护真实指纹的最佳方式是不动它；
        - dialog 处理器自动 dismiss alert/confirm/prompt/beforeunload，
          否则弹窗会挂死后续的 click/evaluate（Playwright 在 dialog 打开期间阻塞协议调用）。
        """
        if not self._context:
            return
        context_id = id(self._context)
        if self._context_hardening_id == context_id:
            return
        taking_over_external = bool((getattr(self.config, "browser_connect_url", "") or "").strip())
        if taking_over_external:
            logger.info("接管外部浏览器模式：跳过 stealth 注入，保留真实浏览器指纹。")
        else:
            try:
                await Stealth().apply_stealth_async(self._context)
            except Exception as e:
                logger.warning("为 context 注入 stealth 脚本失败: %s", e)
        try:
            self._context.on("dialog", self._on_dialog)
        except Exception as e:
            logger.debug("注册 dialog 处理器失败: %s", e)
        # 绑定新 context 后，旧 page 的元素映射全部失效
        self._prune_element_mappings()
        self._context_hardening_id = context_id

    def _on_dialog(self, dialog) -> None:
        """自动 dismiss JS 弹窗并记录内容，供下一次操作结果向 agent 报告。

        注意 handler 必须同步返回，dismiss 用 create_task 异步执行。
        """
        self._last_dialog = (dialog.type, dialog.message)
        logger.info("页面弹出 %s 对话框已自动关闭: %s", dialog.type, dialog.message)
        try:
            asyncio.create_task(self._dismiss_dialog(dialog))
        except RuntimeError:
            # 无运行中的事件循环（仅测试场景可能发生），跳过异步 dismiss
            logger.debug("无法调度 dialog dismiss（无事件循环）")

    async def _dismiss_dialog(self, dialog) -> None:
        try:
            await dialog.dismiss()
        except Exception as e:
            logger.debug("dismiss dialog 失败（可能已随导航销毁）: %s", e)

    # ---------- 元素映射辅助 ----------

    def _page_key(self, page) -> int:
        return id(page)

    def _prune_element_mappings(self) -> None:
        """清理已关闭页面残留的元素映射，并把活动映射切到当前页面（若无则清空）。"""
        if not self._element_mappings:
            return
        valid_keys = {self._page_key(p) for p in self._get_valid_pages()}
        self._element_mappings = {
            k: v for k, v in self._element_mappings.items() if k in valid_keys
        }
        self._active_mapping = (
            self._element_mappings.get(self._page_key(self._page), {})
            if self._page else {}
        )

    def _consume_dialog_report(self) -> str:
        """取出并清空最近一次自动 dismiss 的 dialog 信息，拼进操作返回文案。"""
        if not self._last_dialog:
            return ""
        dtype, message = self._last_dialog
        self._last_dialog = None
        text = message.strip().replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + "..."
        return f"\n[页面弹窗] 页面弹出了 {dtype} 对话框，已自动关闭。内容: {text}"

    # ---------- CAPTCHA 检测 ----------

    async def _detect_human_verification(self) -> str | None:
        """检测人机验证，hard 级才要求人工接管，soft 级记录提示不阻断。

        分级语义（与 dom_scripts._ASSESS_CHALLENGE_JS 一致）：
        - hard：整页插页 / 大面积可见挑战框，必须用户人工完成；
        - soft：页面常驻的防刷组件（reCAPTCHA 小勾选框等），只记录提示，
          agent 继续操作——修复"页面有 reCAPTCHA 组件但没弹验证"的误报劫持。

        缓存复核：observer 上报的缓存值每次被操作触发时重新主动验证，
        主动检测不到就清除缓存（用户已手动完成验证 / 误报自愈），防止
        误报缓存粘住导致 agent 永久停在"请人工验证"。
        """
        if not self._page or self._page.is_closed():
            return None

        try:
            verdict = await self._page.evaluate(DETECT_HUMAN_VERIFICATION_SCRIPT, CAPTCHA_PATTERNS)
        except Exception as exc:
            logger.debug("human verification detection failed: %s", exc)
            # 检测本身失败时保守返回缓存（可能浏览器正在重连，不贸然清除）
            return self._captcha_detected_reason

        if verdict and isinstance(verdict, dict):
            level = verdict.get("level")
            reason = str(verdict.get("reason", ""))
            if level == "hard":
                if self._captcha_detected_reason != reason:
                    logger.warning("human verification detected url=%s reason=%s", self._page.url, reason)
                self._captcha_detected_reason = reason
                return reason
            # soft：不阻断，记录提示供 agent 感知"提交时可能弹验证"
            if level == "soft":
                self._captcha_soft_note = reason
                # 主动复核通过，清掉可能存在的旧 hard 缓存（挑战已消失/误报）
                self._captcha_detected_reason = None
                return None

        # 主动检测无任何信号：挑战已消失，清除所有缓存
        if self._captcha_detected_reason:
            logger.info("人机验证已消失（用户完成或挑战卸载），清除缓存继续操作。")
            self._captcha_detected_reason = None
        self._captcha_soft_note = None
        return None

    # ---------- 页面列表 ----------

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
            # 页面切换后，活动元素映射必须跟着切换
            self._active_mapping = self._element_mappings.get(self._page_key(self._page), {})

        try:
            # 每次执行动作前，强制把 Agent 正在操作的标签页切到最前面
            await self._page.bring_to_front()
        except Exception:
            pass

    # ---------- 状态提取 ----------

    @auto_heal_retry()
    async def get_state(self, session_id: str | None = None) -> str:
        """获取当前页面的完整可访问性树（包含元素 ID），供大模型阅读。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                await self._update_active_page()

                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面，请先使用 browser_navigate 导航到网页。"

                logger.info("获取当前页面状态（Accessibility Tree）...")
                if reason := await self._detect_human_verification():
                    return _human_verification_message(self._page.url, reason)

                # soft 级验证组件提示：常驻防刷组件不算拦截，但提交表单时可能弹挑战
                soft_note = getattr(self, "_captcha_soft_note", None)
                soft_prefix = (
                    f"[页面提示] 检测到页面嵌有防刷验证组件（{soft_note}）。"
                    "当前未被拦截，可正常浏览；若后续提交表单时弹出验证挑战再处理。\n"
                ) if soft_note else ""

                # 先在局部 dict 上构建新的映射，构建完成后再原子发布，避免中间态被并发的 click/type_text 读到。
                new_mapping: dict[int, str] = {}

                # 条件等待：DOM 稳定后提取（替代固定 sleep(0.5)，快页面立即返回）
                await self._wait_dom_quiesce(quiet_ms=400, timeout_ms=2500)

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

                    # 输出层渲染：状态值标记 + 相似兄弟折叠。
                    # 折叠只影响显示（省 token），mapping 始终保留全部 ID（保证可点击）。
                    def _fold_key(el):
                        return (el.get('role'), el.get('type'), el.get('text'))

                    i = 0
                    while i < len(all_els):
                        el = all_els[i]
                        # mapping 注册所有元素（含被折叠的）
                        new_mapping[el['id']] = (el.get('framePath') or [], f"[data-agent-id='{el['id']}']")

                        # 相似折叠：连续 >3 个同 key 的交互元素，只显示首个 + 折叠计数
                        j = i + 1
                        if el.get('isInteractive') and el.get('text'):
                            while j < len(all_els) and _fold_key(all_els[j]) == _fold_key(el):
                                j += 1
                        similar = j - i - 1
                        if similar < 3:
                            j = i + 1  # 低于阈值不折叠，逐个渲染
                            similar = 0

                        type_str     = f" [type:{el['type']}]" if el.get('type') else ""
                        interact_mark = " [*]" if el.get('isInteractive') else ""
                        pos = el.get('position', 'viewport')
                        pos_mark = "" if pos == 'viewport' else (" [↑]" if pos == 'above' else " [↓]")
                        frame_path = el.get('framePath') or []
                        frame_mark = "" if not frame_path else f" [iframe {'>'.join(str(f) for f in frame_path)}]"
                        state = el.get('state')
                        state_mark = f" [当前:{state}]" if state else ""
                        fold_mark = f" [+{similar} 个相同元素已折叠]" if similar >= 3 else ""
                        output.append(
                            f"[{el['id']}]{interact_mark}{pos_mark}{frame_mark} {el['role'].upper()}{type_str}{state_mark}: {el['text']}{fold_mark}"
                        )
                        i = j

                    # 全部构建完成后才发布映射（按页面键存储），确保读到的要么是旧的、要么是新的，没有中间态。
                    page_key = self._page_key(self._page)
                    self._element_mappings[page_key] = new_mapping
                    self._active_mapping = new_mapping
                    return soft_prefix + "\n".join(output) + self._consume_dialog_report()

                except Exception as e:
                    if is_transient_disconnect(e):
                        raise  # 冒泡到 auto_heal_retry 触发自愈重试
                    logger.error("Failed to get state: %s", e)
                    return f"获取页面状态失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    @auto_heal_retry()
    async def extract_content(self, max_chars: int = 60000, include_links: bool = True, session_id: str | None = None) -> str:
        """提取当前页面正文、metadata、标题和链接，供研究任务读取。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                await self._update_active_page()

                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面，请先使用 browser_navigate 导航到网页。"

                logger.info("提取当前页面正文内容...")
                if reason := await self._detect_human_verification():
                    return _human_verification_message(self._page.url, reason)

                # P3-3: 优先 trafilatura（中文/结构化页面效果远好于自研选择器猜谜），
                # 未安装或提取失败时降级原 JS 提取
                extracted = await self._extract_with_trafilatura(max_chars)
                if extracted is not None:
                    return extracted + self._consume_dialog_report()

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
                    return "\n".join(output) + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise  # 冒泡到 auto_heal_retry 触发自愈重试
                    logger.error("Failed to extract page content: %s", e)
                    return f"提取页面正文失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    async def _extract_with_trafilatura(self, max_chars: int) -> str | None:
        """用 trafilatura 提取正文（可选依赖，未安装/失败返回 None 走降级）。

        在工作线程执行（trafilatura 是 CPU 密集纯同步库，避免阻塞事件循环）。
        """
        try:
            import trafilatura  # type: ignore
        except ImportError:
            return None
        try:
            html = await self._page.content()
            if not html:
                return None

            def _sync_extract() -> str | None:
                extracted = trafilatura.extract(
                    html,
                    output_format="txt",
                    include_comments=False,
                    include_tables=True,
                    with_metadata=True,
                    url=self._page.url,
                )
                return extracted

            text = await asyncio.to_thread(_sync_extract)
            if not text or len(text.strip()) < 80:
                return None
            if len(text) > max_chars:
                text = text[:max_chars] + "\n...[已截断]"

            # metadata（trafilatura 的 JSON 输出含更多字段，这里用 bbox 简化）
            import json as _json

            meta_raw = await asyncio.to_thread(
                lambda: trafilatura.extract_metadata(html)
            )
            output = [
                f"URL: {self._page.url}",
                f"Title: {getattr(meta_raw, 'title', '') or ''}",
                f"Author: {getattr(meta_raw, 'author', '') or ''}",
                f"Date: {getattr(meta_raw, 'date', '') or ''}",
                "-" * 40,
                "Content:",
            ]
            output.append(text)
            return "\n".join(output)
        except Exception as e:
            logger.debug("trafilatura extraction failed (fallback to js): %s", e)
            return None
