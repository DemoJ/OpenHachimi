"""浏览器自动化工具。"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps

logger = logging.getLogger(__name__)


async def browser_navigate(ctx: RunContext[AgentDeps], url: str) -> str:
    """导航浏览器到指定的网址。使用该工具前请确保需要访问网页，完成后使用 browser_get_state 获取页面信息。"""
    bm = ctx.deps.browser_manager
    result = await bm.navigate(url)
    return result


async def browser_get_state(ctx: RunContext[AgentDeps]) -> str:
    """获取当前浏览器的页面状态（提取出交互元素的精简树结构和 ID）。

    返回结果将包含：当前URL、页面标题、以及所有交互元素的列表（带 [ID] 前缀）。
    你可以通过阅读这些信息了解页面长什么样，并使用提供的 [ID] 调用 browser_click 或 browser_type。
    """
    bm = ctx.deps.browser_manager
    result = await bm.get_state()
    return result


async def browser_extract_content(ctx: RunContext[AgentDeps], max_chars: int = 60000, include_links: bool = True) -> str:
    """提取当前浏览器页面的正文、metadata、标题和链接。

    适合在 web_fetch 被公开站点拦截、或页面需要浏览器渲染后，用于研究任务读取稳定正文。
    本工具只读取当前页面，不负责导航；如需打开 URL，请先调用 browser_navigate(url)。
    遇到 CAPTCHA / 人机验证 / 权限页面时会停止并报告，不会尝试绕过。
    """
    bm = ctx.deps.browser_manager
    result = await bm.extract_content(max_chars=max_chars, include_links=include_links)
    return result


async def browser_click(ctx: RunContext[AgentDeps], element_id: int) -> str:
    """点击浏览器页面中指定 ID 的元素。
    
    参数 `element_id` 必须是之前调用 browser_get_state 获取到的页面状态中元素前的数字 ID。
    """
    bm = ctx.deps.browser_manager
    result = await bm.click(element_id)
    return result


async def browser_type(ctx: RunContext[AgentDeps], element_id: int, text: str, simulate_typing: bool = False) -> str:
    """在浏览器页面中指定 ID 的输入框（或文本区域）中输入文本。
    
    参数 `element_id` 必须是之前调用 browser_get_state 获取到的页面状态中元素前的数字 ID。
    
    参数 `simulate_typing` (布尔值):
    - 默认为 False。系统会使用极速且原子的方式瞬间填入文本，这是绝大多数表单和长文本的推荐方式。
    - 设为 True。系统将模拟人类真实的逐字敲击。只有当遇到“必须逐字敲击才会触发下拉联想建议”的动态搜索框（例如搜索引擎主页、实时检索列表）时才使用此选项。
    """
    bm = ctx.deps.browser_manager
    result = await bm.type_text(element_id, text, simulate_typing)
    return result


async def browser_scroll(ctx: RunContext[AgentDeps], direction: str, amount: int = 600, element_id: int | None = None) -> str:
    """滚动当前浏览器页面或指定的局部容器。
    
    当 browser_get_state 输出中显示“下方还有 Npx 内容”时，必须调用此工具滚动查看更多内容。
    
    参数：
    - direction: 滚动方向。可选：
        'down'   — 向下滚动（最常用，向下查看更多内容）
        'up'     — 向上滚动
        'bottom' — 直接跳到页面最底部
        'top'    — 直接跳回页面顶部
    - amount: 滚动像素数（仅 up/down 有效，默认 600 约一屏）
    - element_id: 可选。如果明确知道需要滚动的区域（如侧边栏、弹窗列表）而非整个页面，可以传入该容器内部任意已知元素的 ID，系统会自动寻找最近的可滚动祖先并滚动它。如果不传，则默认全局滚动。
    
    滚动完成后必须调用 browser_get_state 查看新视口中的内容。
    """
    bm = ctx.deps.browser_manager
    result = await bm.scroll(direction, amount, element_id)
    return result


async def browser_list_tabs(ctx: RunContext[AgentDeps]) -> str:
    """获取并列出当前打开的所有标签页及其索引。
    
    返回的列表中会包含每个标签页的 [索引] 标题 (URL)，以及标注出哪个是当前活动的标签页。
    当需要知道当前打开了哪些页面，或者需要切换、关闭页面前，应调用此工具。
    """
    bm = ctx.deps.browser_manager
    return await bm.list_tabs()


async def browser_new_tab(ctx: RunContext[AgentDeps], url: str = None) -> str:
    """新建一个空白标签页，并自动切换为活动状态。
    
    参数 `url` 是可选的，如果提供，则会自动导航到该网址。
    使用此工具可以保留原页面的情况下，在新的标签页中打开链接或进行搜索。
    """
    bm = ctx.deps.browser_manager
    return await bm.new_tab(url)


async def browser_switch_tab(ctx: RunContext[AgentDeps], tab_index: int) -> str:
    """切换到指定索引的标签页。
    
    参数 `tab_index` 必须是调用 browser_list_tabs 获取到的有效索引。
    切换后，该页面将变为活动状态，后续的操作（如点击、输入、获取状态）都将作用于此页面。
    """
    bm = ctx.deps.browser_manager
    return await bm.switch_tab(tab_index)


async def browser_close_tab(ctx: RunContext[AgentDeps], tab_index: int = None) -> str:
    """关闭指定索引的标签页。
    
    参数 `tab_index` 是可选的，如果不提供，则默认关闭当前处于活动状态的标签页。
    关闭后，如果有其他打开的标签页，会自动切换到最新的一个。如果全部关闭了，需要再调用 browser_new_tab 或 browser_navigate 重新打开页面。
    """
    bm = ctx.deps.browser_manager
    return await bm.close_tab(tab_index)
