"""浏览器自动化工具。"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.tools.utils import check_prompt_read

logger = logging.getLogger(__name__)

def _ensure_browser_prompt_read(ctx: RunContext[AppConfig]) -> None:
    if not check_prompt_read(ctx, "system_prompts/browser.md"):
        raise PermissionError(
            "🛑 拦截：在使用浏览器工具前，必须首先调用 read_file 读取 openhachimi_agent/system_prompts/browser.md 了解操作指南与反爬规则。"
        )


# 我们在模块级别持有一个全局 browser_manager 的引用，但最好通过延迟初始化
# 或者依赖注入来处理。这里采用延迟初始化的方式：
_browser_manager = None

def _get_browser_manager(config: AppConfig):
    global _browser_manager
    if _browser_manager is None:
        from openhachimi_agent.service.browser import BrowserManager
        _browser_manager = BrowserManager(config)
    return _browser_manager


async def browser_navigate(ctx: RunContext[AppConfig], url: str) -> str:
    """导航浏览器到指定的网址。使用该工具前请确保需要访问网页，完成后使用 browser_get_state 获取页面信息。"""
    _ensure_browser_prompt_read(ctx)
    bm = _get_browser_manager(ctx.deps)
    return await bm.navigate(url)


async def browser_get_state(ctx: RunContext[AppConfig]) -> str:
    """获取当前浏览器的页面状态（提取出交互元素的精简树结构和 ID）。
    
    返回结果将包含：当前URL、页面标题、以及所有交互元素的列表（带 [ID] 前缀）。
    你可以通过阅读这些信息了解页面长什么样，并使用提供的 [ID] 调用 browser_click 或 browser_type。
    """
    _ensure_browser_prompt_read(ctx)
    bm = _get_browser_manager(ctx.deps)
    return await bm.get_state()


async def browser_click(ctx: RunContext[AppConfig], element_id: int) -> str:
    """点击浏览器页面中指定 ID 的元素。
    
    参数 `element_id` 必须是之前调用 browser_get_state 获取到的页面状态中元素前的数字 ID。
    """
    _ensure_browser_prompt_read(ctx)
    bm = _get_browser_manager(ctx.deps)
    return await bm.click(element_id)


async def browser_type(ctx: RunContext[AppConfig], element_id: int, text: str) -> str:
    """在浏览器页面中指定 ID 的输入框（或文本区域）中输入文本。
    
    参数 `element_id` 必须是之前调用 browser_get_state 获取到的页面状态中元素前的数字 ID。
    """
    _ensure_browser_prompt_read(ctx)
    bm = _get_browser_manager(ctx.deps)
    return await bm.type_text(element_id, text)


async def browser_scroll(ctx: RunContext[AppConfig], direction: str, amount: int = 600) -> str:
    """滚动当前浏览器页面。
    
    当 browser_get_state 输出中显示“下方还有 Npx 内容”时，必须调用此工具滚动查看更多内容。
    
    参数：
    - direction: 滚动方向。可选：
        'down'   — 向下滚动（最常用，向下查看更多内容）
        'up'     — 向上滚动
        'bottom' — 直接跳到页面最底部
        'top'    — 直接跳回页面顶部
    - amount: 滚动像素数（仅 up/down 有效，默认 600 约一屏）
    
    滚动完成后必须调用 browser_get_state 查看新视口中的内容。
    """
    _ensure_browser_prompt_read(ctx)
    bm = _get_browser_manager(ctx.deps)
    return await bm.scroll(direction, amount)
