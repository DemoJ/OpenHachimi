"""页面交互操作 Mixin：click / type_text / scroll / press_key / select_option / hover / wait_for。

依赖宿主类提供（见 BrowserManager）：
- self._page / self._active_mapping / self._op_lock
- self._record_activity() / self._update_active_page()
- self._detect_human_verification() / self._consume_dialog_report()
- auto_heal_retry / is_transient_disconnect（从 retry.py 导入）
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .dom_scripts import (
    DOM_QUIESCE_SCRIPT,
    ELEMENT_CENTER_SCRIPT,
    ELEMENT_STATE_SCRIPT,
    PAGE_SNAPSHOT_SCRIPT,
    SCROLL_INTO_CONTAINER_SCRIPT,
    SELECT_OPTIONS_SCRIPT,
    SENSITIVE_FORM_DETECT_SCRIPT,
    WAIT_FOR_SCRIPT,
)
from .retry import auto_heal_retry, is_transient_disconnect
from .utils import _human_verification_message

logger = logging.getLogger(__name__)


def _bezier_trajectory(start: tuple[float, float], end: tuple[float, float], steps: int | None = None) -> list[tuple[float, float]]:
    """二次贝塞尔鼠标轨迹（browser-use 同款思路）。

    - 控制点取起终点中点 + 随机偏移，形成自然弧线而非直线瞬移；
    - 步数随距离自适应，每步坐标再叠加小随机抖动；
    - 事件间 delay 由调用方控制（含缓动：先快后慢）。
    """
    import math

    (x0, y0), (x1, y1) = start, end
    dist = math.hypot(x1 - x0, y1 - y0)
    if steps is None:
        steps = max(8, min(35, int(dist / 25)))
    # 控制点：中点偏移，垂直方向偏移量与距离正相关
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = x1 - x0, y1 - y0
    norm = math.hypot(dx, dy) or 1.0
    offset = min(dist * 0.3, 120)
    cx, cy = mx - dy / norm * offset * random.uniform(0.4, 1.0), my + dx / norm * offset * random.uniform(0.4, 1.0)

    points = []
    for i in range(1, steps + 1):
        # ease-out：前段快后段慢（人类减速瞄准）
        t = 1 - (1 - i / steps) ** 2
        bx = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1
        by = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1
        jitter = max(0.5, 2.0 - i / steps)
        bx += random.uniform(-jitter, jitter)
        by += random.uniform(-jitter, jitter)
        points.append((bx, by))
    return points


class BrowserInteractionsMixin:
    """面向 LLM 工具的页面交互操作。"""

    async def _resolve_locator(self, selector_entry: tuple[list[int], str]):
        """把 (framePath, css_selector) 解析为 Playwright Locator。

        framePath 空 = 主文档；[1] = data-agent-frame=1 的 iframe；
        [1,2] = 第 1 个 iframe 内 data-agent-frame=2 的嵌套 iframe。
        通过 frame_locator 链逐层穿透，与 frame 挂载顺序无关，天然支持嵌套。
        """
        frame_path, css = selector_entry
        scope = self._page
        for fid in frame_path or []:
            scope = scope.frame_locator(f"iframe[data-agent-frame='{fid}']")
        return scope.locator(css).first

    async def _resolve_frame(self, frame_path: list[int]):
        """把 framePath 解析为 Playwright Frame 对象（供 frame 内 evaluate）。"""
        frame = self._page.main_frame
        for fid in frame_path or []:
            handle = await frame.locator(f"iframe[data-agent-frame='{fid}']").first.element_handle()
            if handle is None:
                raise RuntimeError(f"找不到编号为 {fid} 的 iframe（页面可能已变化），请重新 browser_get_state。")
            child = await handle.content_frame()
            if child is None:
                raise RuntimeError(f"iframe {fid} 无 content frame（可能已卸载），请重新 browser_get_state。")
            frame = child
        return frame

    async def _evaluate_in_frame(self, frame_path: list[int], script: str, arg):
        """在指定 frame 路径的文档中执行 evaluate（framePath 空 = 主文档）。"""
        frame = await self._resolve_frame(frame_path)
        return await frame.evaluate(script, arg)

    # ---------- P2: 页面变化摘要 / 条件等待 / 描述校验 ----------

    async def _page_snapshot(self) -> dict:
        """click 前后对比用的轻量页面快照。"""
        try:
            return await self._page.evaluate(PAGE_SNAPSHOT_SCRIPT) or {}
        except Exception:
            return {}

    def _diff_snapshot(self, before: dict, after: dict) -> str:
        """对比两次快照，产出供 agent 决策的变化摘要。"""
        if not before or not after:
            return ""
        parts = []
        if before.get("url") != after.get("url"):
            parts.append(f"页面已跳转到 {after.get('url', '')}")
        else:
            if before.get("title") != after.get("title"):
                parts.append(f"标题变为「{after.get('title', '')}」")
            delta = (after.get("interactive") or 0) - (before.get("interactive") or 0)
            if abs(delta) >= 3:
                parts.append(f"交互元素 {'增加' if delta > 0 else '减少'} {abs(delta)} 个（可能展开了菜单/弹窗）")
            elif delta:
                parts.append(f"交互元素 {delta:+d} 个")
        return "；".join(parts)

    async def _wait_dom_quiesce(self, quiet_ms: int = 400, timeout_ms: int = 3000) -> bool:
        """等待 DOM 稳定（quiet_ms 无变更）。返回是否在超时内收敛。"""
        try:
            return bool(await self._page.evaluate(DOM_QUIESCE_SCRIPT, {"quietMs": quiet_ms, "timeoutMs": timeout_ms}))
        except Exception as e:
            logger.debug("wait_dom_quiesce failed: %s", e)
            return False

    async def _verify_element_description(self, element_id: int, description: str | None, selector_entry) -> str | None:
        """Skyvern 式描述自检：校验元素当前文本与 agent 给出的 description 是否相符。

        相符返回 None；明显不符返回错误文案（agent 应重新 get_state）。
        宽松策略：元素文本与 description 互不包含且无公共子串（>=2 字符）才判为不符，
        避免动态文本（计数、时间戳）造成误杀。
        """
        if not description:
            return None
        try:
            state = await self._evaluate_in_frame(selector_entry[0], ELEMENT_STATE_SCRIPT, selector_entry[1])
            el_value = ""
            el_text = ""
            if isinstance(state, dict):
                el_value = str(state.get("value") or state.get("text") or "")
            # 再取元素整体文本（aria/innerText 层面）
            text_script = "(s) => { const el = document.querySelector(s); return el ? (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '') : null; }"
            el_text = str(await self._evaluate_in_frame(selector_entry[0], text_script, selector_entry[1]) or "")
            candidates = [t for t in (description, el_value, el_text) if t]
            if len(candidates) < 2:
                return None
            desc, *actuals = candidates
            def _norm(s: str) -> str:
                return "".join(ch for ch in s.lower() if not ch.isspace())
            desc_n = _norm(desc)
            for actual in actuals:
                actual_n = _norm(actual)
                if not actual_n:
                    continue
                if desc_n in actual_n or actual_n in desc_n:
                    return None  # 任一实际值与描述互含即通过
            return (
                f"元素 [{element_id}] 的实际内容与描述「{description}」不符"
                f"（实际：{el_text[:60] or el_value[:60] or '空'}）。页面可能已变化，请重新 browser_get_state。"
            )
        except Exception as e:
            logger.debug("verify description failed (非致命): %s", e)
            return None

    # ---------- P3: 遮挡处理 ----------

    async def _human_move_click(self, selector_entry) -> bool:
        """贝塞尔轨迹点击：CDP mouse 事件（trusted）+ 人类化移动。

        成功返回 True；元素不可定位/坐标异常返回 False（调用方降级 locator.click）。
        """
        try:
            frame_path, css = selector_entry
            info = await self._evaluate_in_frame(frame_path, ELEMENT_CENTER_SCRIPT, css)
            if not info or not isinstance(info, dict):
                return False
            # 嵌套 frame 的坐标是相对该 iframe 的，CDP 需要绝对页面坐标；
            # 只有主文档元素可直接用（iframe 场景降级 locator.click）
            if frame_path:
                return False
            x, y = info["x"], info["y"]
            vw, vh = info.get("viewportW", 1280), info.get("viewportH", 800)
            if not (0 <= x < vw and 0 <= y < vh):
                return False

            # 起点：视口内随机位置（模拟鼠标当前位置）
            start = (random.uniform(0, vw) * 0.8 + vw * 0.1, random.uniform(0, vh) * 0.8 + vh * 0.1)
            mouse = self._page.mouse
            await mouse.move(*start)
            await asyncio.sleep(random.uniform(0.05, 0.15))

            for px, py in _bezier_trajectory(start, (x, y)):
                await mouse.move(px, py)
                await asyncio.sleep(random.uniform(0.005, 0.02))
            await asyncio.sleep(random.uniform(0.03, 0.1))
            await mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.12))  # 按住时长（人类 60-120ms）
            await mouse.up()
            return True
        except Exception as e:
            logger.debug("human move click failed (fallback to locator.click): %s", e)
            return False

    async def _scroll_element_into_view(self, selector_entry) -> None:
        """把元素滚动进可视区（处理视口外元素点击超时的常见原因）。"""
        try:
            frame = await self._resolve_frame(selector_entry[0])
            await frame.evaluate(
                "(s) => { const el = document.querySelector(s); if (el) el.scrollIntoView({block: 'center', behavior: 'instant'}); }",
                selector_entry[1],
            )
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.debug("scroll_into_view failed: %s", e)

    async def _detect_occluder(self, selector_entry) -> str | None:
        """force click 前检测遮挡元素：取目标中心点 elementFromPoint 的最外层非目标元素描述。"""
        try:
            frame = await self._resolve_frame(selector_entry[0])
            info = await frame.evaluate(
                """(s) => {
                    const el = document.querySelector(s);
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    const cx = Math.round(r.left + r.width / 2);
                    const cy = Math.round(r.top + r.height / 2);
                    const hit = document.elementFromPoint(cx, cy);
                    if (!hit || el.contains(hit) || hit.contains(el)) return null;
                    const desc = (hit.getAttribute('aria-label') || hit.innerText || hit.tagName || '').trim();
                    const cls = (typeof hit.className === 'string' ? hit.className : '').split(/\\s+/).slice(0, 2).join('.');
                    return (desc || cls || hit.tagName.toLowerCase()).slice(0, 60);
                }""",
                selector_entry[1],
            )
            return info or None
        except Exception as e:
            logger.debug("detect occluder failed: %s", e)
            return None

    @auto_heal_retry()
    async def click(self, element_id: int, description: str | None = None, session_id: str | None = None) -> str:
        """点击指定 ID 的元素。

        description 可选：传入元素描述（如"搜索按钮"）用于自检，若与元素实际内容
        明显不符会返回错误而非点错元素。
        """
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                if element_id not in self._active_mapping:
                    return f"找不到 ID 为 {element_id} 的元素，请先调用 browser_get_state 刷新状态。"

                selector_entry = self._active_mapping[element_id]
                logger.info("Browser clicking element_id=%d selector=%s", element_id, selector_entry)

                # 描述自检：不符则拒绝点击（防 DOM 变化后点错元素）
                if mismatch := await self._verify_element_description(element_id, description, selector_entry):
                    return mismatch

                before_tabs = {id(p) for p in self._get_valid_pages()}
                snapshot_before = await self._page_snapshot()
                url_before = self._page.url

                await asyncio.sleep(random.uniform(0.1, 0.3))
                occluder = None
                # 优先贝塞尔轨迹点击（CDP trusted 事件 + 人类化移动）；
                # iframe 内元素 / 坐标异常时降级 locator.click
                human_clicked = await self._human_move_click(selector_entry)
                if not human_clicked:
                    locator = await self._resolve_locator(selector_entry)
                    try:
                        await locator.click(timeout=5000, delay=random.randint(10, 50))
                    except PlaywrightTimeout:
                        # 超时通常是被遮挡/不可点击：先滚动进视口再试一次（不 force）
                        await self._scroll_element_into_view(selector_entry)
                        await locator.click(timeout=3000, delay=random.randint(10, 50))
                    except Exception:
                        # 最后手段：force 点击并报告遮挡元素（P3-2）
                        occluder = await self._detect_occluder(selector_entry)
                        await locator.click(timeout=3000, force=True, delay=random.randint(10, 50))
                        if occluder:
                            logger.warning("force click used, occluded by: %s", occluder)

                # 条件等待：DOM 稳定即继续（快页面 0.4s，慢页面等到 3s 上限），
                # 替代原固定 sleep(1.0)
                await self._wait_dom_quiesce(quiet_ms=400, timeout_ms=3000)
                await self._update_active_page()

                # 页面变化摘要（P2）：让 agent 免掉一次 get_state 往返
                changes = []
                new_tabs = [p for p in self._get_valid_pages() if id(p) not in before_tabs]
                if new_tabs:
                    changes.append(f"打开了新标签页：{new_tabs[0].url}")
                    self._bind_session_page(session_id, self._page)
                else:
                    snapshot_after = await self._page_snapshot()
                    diff = self._diff_snapshot(snapshot_before, snapshot_after)
                    if diff:
                        changes.append(diff)
                if self._page.url != url_before and not any("跳转" in c for c in changes):
                    changes.append(f"URL 变为 {self._page.url}")

                if reason := await self._detect_human_verification():
                    return _human_verification_message(self._page.url, reason)

                summary = f"成功点击元素 [{element_id}]。"
                if changes:
                    summary += "变化：" + "；".join(changes) + "。"
                if occluder:
                    summary += (
                        f"注意：该元素被「{occluder}」遮挡，已强制点击。"
                        "若点击未生效，请先处理遮挡物（如关闭 cookie 提示条/弹窗）后重试。\n"
                    )
                summary += "如需查看新内容请调用 browser_get_state。"
                return summary + self._consume_dialog_report()
            except Exception as e:
                if is_transient_disconnect(e):
                    raise  # 冒泡到 auto_heal_retry 触发自愈重试
                return f"点击失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    @auto_heal_retry()
    async def type_text(self, element_id: int, text: str, simulate_typing: bool = False, description: str | None = None, session_id: str | None = None) -> str:
        """在指定 ID 的输入框中输入文本。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                await self._update_active_page()

                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                if element_id not in self._active_mapping:
                    return f"找不到 ID 为 {element_id} 的元素，请先调用 browser_get_state 刷新状态。"

                selector_entry = self._active_mapping[element_id]
                logger.info("Browser typing text in element_id=%d selector=%s simulate_typing=%s", element_id, selector_entry, simulate_typing)

                # 描述自检：不符则拒绝输入（防 DOM 变化后填错输入框）
                if mismatch := await self._verify_element_description(element_id, description, selector_entry):
                    return mismatch

                locator = await self._resolve_locator(selector_entry)

                # 敏感表单检测：密码框 / 登录容器内 / autocomplete 提示，
                # 即使未显式要求也自动切换逐字输入（trusted 键盘事件）。
                # fill() 走 JS 设值派发的是 isTrusted:false 合成事件，
                # X/Cloudflare 等风控专门检测，登录场景最致命。
                effective_typing = simulate_typing
                if not effective_typing:
                    try:
                        sensitive = await self._evaluate_in_frame(
                            selector_entry[0], SENSITIVE_FORM_DETECT_SCRIPT, selector_entry[1]
                        )
                        if sensitive:
                            effective_typing = True
                            logger.info("element_id=%d 在登录/敏感表单中，自动使用逐字输入", element_id)
                    except Exception:
                        pass

                if not effective_typing:
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
                return f"成功在元素 [{element_id}] 输入文本。" + self._consume_dialog_report()
            except Exception as e:
                if is_transient_disconnect(e):
                    raise  # 冒泡到 auto_heal_retry 触发自愈重试
                return f"输入文本失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    @auto_heal_retry()
    async def press_key(self, key: str, element_id: int | None = None, session_id: str | None = None) -> str:
        """按下键盘键。可传 element_id 聚焦到元素后按键，否则对当前页面直接按键。

        key 支持 Playwright 键名：'Enter', 'Tab', 'Escape', 'ArrowDown',
        'Backspace', 'Control+A', 'Shift+Tab' 等。
        """
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                await self._update_active_page()

                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                key = key.strip()
                if not key:
                    return "参数 key 不能为空。支持示例：Enter / Tab / Escape / ArrowDown / Control+A"

                try:
                    if element_id is not None:
                        if element_id not in self._active_mapping:
                            return f"找不到 ID 为 {element_id} 的元素，请先调用 browser_get_state 刷新状态。"
                        selector_entry = self._active_mapping[element_id]
                        logger.info("Browser pressing key=%s on element_id=%d", key, element_id)
                        locator = await self._resolve_locator(selector_entry)
                        await locator.press(key, timeout=5000)
                    else:
                        logger.info("Browser pressing key=%s on page", key)
                        await self._page.keyboard.press(key)

                    await asyncio.sleep(0.5)
                    if reason := await self._detect_human_verification():
                        return _human_verification_message(self._page.url, reason)
                    target = f"元素 [{element_id}]" if element_id is not None else "当前页面"
                    return f"已在{target}上按下 {key}。" + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise
                    return f"按键失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    @auto_heal_retry()
    async def select_option(self, element_id: int, option: str, session_id: str | None = None) -> str:
        """选择原生 <select> 下拉框的选项。

        option 按优先级匹配：选项文本 -> value 属性 -> label。
        """
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                await self._update_active_page()

                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                if element_id not in self._active_mapping:
                    return f"找不到 ID 为 {element_id} 的元素，请先调用 browser_get_state 刷新状态。"

                selector_entry = self._active_mapping[element_id]
                logger.info("Browser selecting option='%s' in element_id=%d", option, element_id)

                try:
                    locator = await self._resolve_locator(selector_entry)
                    css = selector_entry[1]

                    # 先读取全部选项供失败时提示（也让 agent 无需提前 get_state 就能拿到候选）
                    opts = await self._evaluate_in_frame(selector_entry[0], SELECT_OPTIONS_SCRIPT, css)
                    if opts is None:
                        return f"元素 [{element_id}] 不是原生 <select> 下拉框。如果是自定义组件，请用 browser_click 展开后点击选项。"

                    match = next(
                        (o for o in opts if o["text"] == option or o["value"] == option),
                        None,
                    )
                    if match is None:
                        # 宽松匹配：忽略大小写与首尾空白
                        match = next(
                            (o for o in opts if o["text"].strip().lower() == option.strip().lower()
                             or o["value"].strip().lower() == option.strip().lower()),
                            None,
                        )
                    if match is None:
                        preview = "; ".join(f"{o['text']}({o['value']})" for o in opts[:20])
                        return (
                            f"下拉框中没有匹配“{option}”的选项。可用选项（text(value)）：{preview}"
                        )

                    await locator.select_option(match["value"], timeout=5000)
                    await asyncio.sleep(0.3)
                    if reason := await self._detect_human_verification():
                        return _human_verification_message(self._page.url, reason)
                    return f"已选择 [{element_id}] 的选项：{match['text']}。" + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise
                    return f"选择选项失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    @auto_heal_retry()
    async def hover(self, element_id: int, session_id: str | None = None) -> str:
        """悬停到指定 ID 的元素上（触发悬停菜单/提示）。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                await self._update_active_page()

                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                if element_id not in self._active_mapping:
                    return f"找不到 ID 为 {element_id} 的元素，请先调用 browser_get_state 刷新状态。"

                selector_entry = self._active_mapping[element_id]
                logger.info("Browser hovering element_id=%d", element_id)

                try:
                    locator = await self._resolve_locator(selector_entry)
                    await locator.hover(timeout=5000)
                    await asyncio.sleep(0.5)
                    if reason := await self._detect_human_verification():
                        return _human_verification_message(self._page.url, reason)
                    return f"已悬停到元素 [{element_id}]。如悬停菜单已展开，请用 browser_get_state 获取新出现的元素。" + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise
                    return f"悬停失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    @auto_heal_retry()
    async def wait_for(self, text: str | None = None, element_id: int | None = None, seconds: float = 3.0, timeout: float = 10.0, session_id: str | None = None) -> str:
        """等待页面出现指定文本，或等待固定秒数（供懒加载/动画收敛）。

        - text：等待页面包含该文本（不区分大小写）。
        - seconds：当不传 text 时，直接等待这么多秒（0.1 ~ 10）。
        - timeout：等待 text 出现的最大秒数，超时返回提示（不算错误）。
        """
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                try:
                    if text:
                        logger.info("Browser waiting for text='%s' timeout=%.1fs", text, timeout)
                        found = await self._page.evaluate(WAIT_FOR_SCRIPT, {"text": text, "timeoutMs": int(timeout * 1000)})
                        if found:
                            return f"页面已出现文本“{text}”。" + self._consume_dialog_report()
                        return f"等待超时（{timeout}s）：页面未出现文本“{text}”。可用 browser_get_state 查看当前页面。"
                    else:
                        seconds = max(0.1, min(seconds, 10.0))
                        await asyncio.sleep(seconds)
                        return f"已等待 {seconds}s。" + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise
                    return f"等待失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    @auto_heal_retry()
    async def screenshot(self, question: str | None = None, session_id: str | None = None) -> str:
        """截取当前页面视口截图，保存为 PNG；配置了视觉模型时自动生成画面描述。

        question 可选：针对截图向视觉模型提出的问题（如"验证码是什么"）。
        """
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                await self._update_active_page()

                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                try:
                    import time as _time

                    shots_dir = self.config.base_dir / "screenshots"
                    shots_dir.mkdir(parents=True, exist_ok=True)
                    fname = f"screenshot_{int(_time.time() * 1000)}.png"
                    path = shots_dir / fname
                    await self._page.screenshot(path=str(path), timeout=10000)
                    logger.info("Browser screenshot saved: %s", path)

                    parts = [f"截图已保存：{path}"]

                    # 视觉描述：配置了 fallback 视觉模型时自动生成
                    desc = await self._describe_screenshot(path, question)
                    if desc:
                        parts.append("[截图画面描述]\n" + desc)
                    else:
                        parts.append("未配置视觉模型，无法自动描述画面。可用 inspect_image 查看元数据。")
                    return "\n".join(parts) + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise
                    logger.error("Screenshot failed: %s", e)
                    return f"截图失败：{e}" + self._consume_dialog_report()
            finally:
                restore()

    async def _describe_screenshot(self, path, question: str | None) -> str | None:
        """调用 fallback 视觉模型描述截图；未配置或失败时返回 None（非致命）。"""
        try:
            vision = getattr(self.config, "vision", None)
            if not vision or not getattr(vision, "enabled", False):
                return None
            if not (getattr(vision, "model", "") and getattr(vision, "api_key", "")):
                return None
            from openhachimi_agent.vision.openai_compatible import VisionImagePayload, request_vision

            prompt = "描述这个网页截图的主要内容、布局和值得注意的元素。"
            if question:
                prompt = f"{prompt}\n重点关注并回答：{question}"
            payload = VisionImagePayload(path=path, content_type="image/png")
            return await request_vision(vision, payload, prompt)
        except Exception as e:
            logger.warning("screenshot vision description failed: %s", e)
            return None

    @auto_heal_retry()
    async def scroll(self, direction: str, amount: int = 600, element_id: int | None = None, session_id: str | None = None) -> str:
        """滚动页面或局部容器。"""
        async with self._op_lock:
            self._record_activity()
            restore = self._session_route(session_id)
            try:
                await self._update_active_page()

                if not self._page or self._page.is_closed():
                    return "当前没有打开的页面。"

                direction = direction.strip().lower()
                if direction not in ("up", "down", "top", "bottom"):
                    return f"不支持的滚动方向：{direction}，请使用 up / down / top / bottom。"

                try:
                    if element_id is not None:
                        if element_id not in self._active_mapping:
                            return f"找不到 ID 为 {element_id} 的元素，无法进行局部滚动。请先调用 browser_get_state 刷新状态或直接进行全局滚动。"
                        selector_entry = self._active_mapping[element_id]

                        success = await self._evaluate_in_frame(
                            selector_entry[0],
                            SCROLL_INTO_CONTAINER_SCRIPT,
                            {"selector": selector_entry[1], "direction": direction, "amount": amount},
                        )
                        if not success:
                            return "找不到该元素或局部滚动失败，可能是页面 DOM 发生了变化。"

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

                    # 条件等待：滚动触发的懒加载/重排稳定后返回（替代固定 sleep(0.8)）
                    await self._wait_dom_quiesce(quiet_ms=400, timeout_ms=2000)
                    logger.info("Browser scroll direction=%s amount=%d element_id=%s", direction, amount, element_id)
                    if reason := await self._detect_human_verification():
                        return _human_verification_message(self._page.url, reason)
                    return result_msg + " 请调用 browser_get_state 查看滚动后的页面内容。" + self._consume_dialog_report()
                except Exception as e:
                    if is_transient_disconnect(e):
                        raise  # 冒泡到 auto_heal_retry 触发自愈重试
                    logger.error("Scroll failed: %s", e)
                    return f"滚动失败：{e}" + self._consume_dialog_report()
            finally:
                restore()
