"""深度搜索与降级抓取工具。"""

import logging
import asyncio
from typing import Literal

from pydantic_ai import RunContext
from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.web import web_fetch

logger = logging.getLogger(__name__)

async def _api_search_raw(query: str) -> list[dict]:
    """Level 1: DuckDuckGo API Search (Returns raw results)"""
    try:
        from ddgs import DDGS
        
        def _do_search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=4))
                
        return await asyncio.to_thread(_do_search)
    except Exception as e:
        logger.warning("API Search failed for '%s': %s", query, e)
        return []

async def _http_fetch(ctx: RunContext[AgentDeps], url: str) -> str | None:
    """Level 2: HTTP Fetch (web_fetch)"""
    try:
        def _do_fetch():
            return web_fetch(ctx, url)
        
        res = await asyncio.to_thread(_do_fetch)
        if "Fetch failed" in res and "Hint" in res:
            return None # Anti-bot or forbidden, requires browser
        return res
    except Exception as e:
        logger.warning("HTTP Fetch failed for '%s': %s", url, e)
        return None

async def _browser_fetch(ctx: RunContext[AgentDeps], url: str) -> str | None:
    """Level 3: Headless Browser Fetch"""
    try:
        from openhachimi_agent.tools.browser import _get_browser_manager
        bm = _get_browser_manager(ctx.deps.config)
        await bm.navigate(url)
        await asyncio.sleep(2)
        state = await bm.get_state()
        return state
    except Exception as e:
        logger.warning("Browser Fetch failed for '%s': %s", url, e)
        return None


async def deep_search(ctx: RunContext[AgentDeps], query: str, target_urls: list[str] | None = None, source_type: Literal["general", "tech", "news"] = "general") -> str:
    """
    深度搜索与网页抓取工具 (Deep Search Tool)
    
    用于执行单次强大的搜索引擎查询，并自带三级降级抓取机制（API -> HTTP -> Browser）。
    当你需要搜索特定信息时调用此工具。如果需要执行多次搜索以完成一个复杂任务，请先使用 create_todos 规划 TODO 列表，
    然后再多次调用此工具。

    【信息检索原则】：务必查阅顶部的当前真实时间。严禁在搜索词中编造或使用过期的年份，必须准确基于当前年份和月份进行检索。

    参数：    - query: 具体的搜索关键词或短语
    - target_urls: （可选）如果你确切知道目标页面的 URL，可以填在这里，底层会直接尝试抓取这些页面。
    - source_type: 数据源偏好 ("general" 普通搜索, "tech" 针对 GitHub/HackerNews 等技术源, "news" 新闻源)
    """
    actual_query = query
    if source_type == "tech":
        if "github.com" not in actual_query and "news.ycombinator.com" not in actual_query:
            actual_query += " (site:github.com OR site:news.ycombinator.com OR site:stackoverflow.com)"
            
    logger.info("Executing Deep Search for query: %s", actual_query)
    
    # 1. API Level - Get Links
    api_results = await _api_search_raw(actual_query)
    
    urls_to_fetch = list(target_urls) if target_urls else []
    for r in api_results:
        href = r.get("href")
        if href and href not in urls_to_fetch:
            urls_to_fetch.append(href)
            
    # Limit to top 3 URLs to avoid excessively long execution
    urls_to_fetch = urls_to_fetch[:3]
    
    fetched_contents = []
    for url in urls_to_fetch:
        logger.info("Fetching URL: %s", url)
        
        # 2. HTTP Level
        content = await _http_fetch(ctx, url)
        
        # 3. Browser Level
        if content is None:
            logger.info("HTTP Fetch failed or blocked, falling back to Browser for URL: %s", url)
            content = await _browser_fetch(ctx, url)
            
        if content:
            # Truncate each fetched content to leave room for others
            fetched_contents.append(f"--- URL: {url} ---\n{content[:8000]}")
            
    if not fetched_contents:
        if api_results:
            snippets = "\n".join(f"Title: {r.get('title')}\nURL: {r.get('href')}\nSnippet: {r.get('body')}" for r in api_results)
            return f"未能成功抓取任何完整网页，仅提供搜索引擎摘要：\n{snippets}"
        return "所有搜索和网页抓取途径均失败。"
        
    return f"针对 '{actual_query}' 的深度搜索结果：\n\n" + "\n\n".join(fetched_contents)
