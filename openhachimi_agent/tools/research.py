"""网页搜索工具（DuckDuckGo）。

职责：调用 DuckDuckGo 搜索引擎，返回相关链接和摘要列表。
仅做搜索，不做页面内容抓取。

工具链建议（信息获取的标准流程）：
  1. web_search(query)         → 获取搜索结果列表（链接 + 摘要）
  2. web_fetch(url)            → HTTP 抓取具体 URL 的页面文本
  3. browser_navigate(url)     → HTTP 被拦截时，用浏览器访问
  4. browser_get_state()       → 读取浏览器当前页面内容
"""

import asyncio
import logging
from typing import Literal

from pydantic_ai import RunContext

from openhachimi_agent.core.deps import AgentDeps

logger = logging.getLogger(__name__)


async def web_search(
    ctx: RunContext[AgentDeps],
    query: str,
    max_results: int = 5,
    source_type: Literal["general", "tech", "news"] = "general",
) -> str:
    """使用 DuckDuckGo 搜索引擎查询信息，返回相关网页的标题、链接和摘要。

    本工具只做搜索，不读取页面完整内容。
    获取到链接后，请根据需要调用 web_fetch（HTTP 抓取）或 browser_navigate（浏览器访问）读取具体页面。

    【使用时机】：
    - 需要查找某个话题的相关资料、新闻、文档时
    - 不确定目标 URL、需要先找到正确页面再读取时
    - 对已知 URL 直接调用 web_fetch 即可，无需先搜索

    【信息检索原则】：务必参考当前真实时间，严禁使用错误年份检索。

    参数：
    - query: 搜索关键词或短语
    - max_results: 返回结果数量，默认 5（最多 10）
    - source_type: 搜索偏好
        * "general" - 通用搜索
        * "tech"    - 技术源（GitHub / HackerNews / Stack Overflow）
        * "news"    - 新闻源
    """
    del ctx  # 当前不需要 deps，保留签名以便将来扩展

    actual_query = query.strip()
    max_results = max(1, min(max_results, 10))

    if source_type == "tech":
        if not any(s in actual_query for s in ("github.com", "stackoverflow.com", "news.ycombinator.com")):
            actual_query += " (site:github.com OR site:news.ycombinator.com OR site:stackoverflow.com)"
    elif source_type == "news":
        if "site:" not in actual_query:
            actual_query += " when:month"  # 偏向近期新闻

    logger.info("web_search query=%r source_type=%s max_results=%d", actual_query, source_type, max_results)

    def _do_search() -> list[dict]:
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                return list(ddgs.text(actual_query, max_results=max_results))
        except Exception as exc:
            logger.warning("DuckDuckGo 搜索失败: %s", exc)
            return []

    results = await asyncio.to_thread(_do_search)

    if not results:
        return f"搜索 '{query}' 未返回任何结果。请尝试换用不同的关键词，或直接用 web_fetch / browser_navigate 访问已知 URL。"

    lines = [f"搜索 '{query}' 共返回 {len(results)} 条结果：\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title") or "（无标题）"
        url = r.get("href") or ""
        snippet = r.get("body") or "（无摘要）"
        lines.append(f"{i}. **{title}**")
        if url:
            lines.append(f"   URL: {url}")
        lines.append(f"   摘要: {snippet}")
        lines.append("")

    lines.append("如需读取上述页面的完整内容，请调用 web_fetch(url) 或 browser_navigate(url)。")
    return "\n".join(lines)
