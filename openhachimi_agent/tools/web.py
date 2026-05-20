"""Lightweight public web resource tools.

These tools are intentionally simpler than the browser tools. They are useful
for public HTML, JSON, RSS, Atom, and documented API endpoints, and should be
tried before opening a browser unless the user explicitly asks for browser use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from pydantic_ai import RunContext

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.utils import trim_output


logger = logging.getLogger(__name__)

MAX_WEB_RESPONSE_CHARS = 60000
WEB_TIMEOUT_SECONDS = 20
WEB_USER_AGENT = "OpenHachimi-Agent/0.1 (+https://github.com/DemoJ/OpenHachimi)"


class WebFetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ResourceLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.title = ""
        self._in_title = False
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "title":
            self._in_title = True
            return

        if tag.lower() == "link":
            rel = attrs_dict.get("rel", "").lower()
            link_type = attrs_dict.get("type", "").lower()
            href = attrs_dict.get("href", "")
            if href and (
                "alternate" in rel
                or "application/rss+xml" in link_type
                or "application/atom+xml" in link_type
                or "application/json" in link_type
            ):
                self.links.append(
                    {
                        "kind": "link",
                        "rel": rel,
                        "type": link_type,
                        "title": attrs_dict.get("title", ""),
                        "url": urljoin(self.base_url, href),
                    }
                )

        if tag.lower() == "a":
            href = attrs_dict.get("href", "")
            if not href:
                return
            lowered = href.lower()
            text_hint = " ".join(
                attrs_dict.get(key, "")
                for key in ("title", "aria-label")
                if attrs_dict.get(key)
            )
            if any(token in lowered for token in ("/api", "rss", "feed", "atom", ".json", ".xml")):
                self.links.append(
                    {
                        "kind": "a",
                        "rel": "",
                        "type": "",
                        "title": text_hint,
                        "url": urljoin(self.base_url, href),
                    }
                )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()


def _validate_public_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("url 不能为空")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"仅支持 http/https URL：{url}")
    return url


def _request_url(url: str) -> tuple[str, str, str]:
    request = Request(url, headers={"User-Agent": WEB_USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(request, timeout=WEB_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("content-type", "")
            final_url = response.geturl()
            raw = response.read(MAX_WEB_RESPONSE_CHARS + 1)
    except HTTPError as exc:
        body = exc.read(12000).decode("utf-8", errors="replace")
        raise WebFetchError(f"HTTP {exc.code} {exc.reason}: {body}", status_code=exc.code) from exc
    except URLError as exc:
        raise WebFetchError(f"请求失败：{exc}") from exc

    charset = "utf-8"
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    text = raw.decode(charset, errors="replace")
    return final_url, content_type, text


def _maybe_pretty_json(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def web_fetch(ctx: RunContext[AgentDeps], url: str) -> str:
    """通过 HTTP 抓取指定 URL 的公开页面或 API 内容（不启动浏览器）。

    适用于公开 HTML 页面、JSON API、RSS、Atom 等文本端点。
    如果返回 'Fetch failed' + 'Hint'，说明该页面有反爬保护，请改用 browser_navigate。

    【工具选择建议】：
    - 先用 web_search 获取相关链接，再用本工具读取页面内容
    - 若本工具失败（403 / 429 等），升级到 browser_navigate + browser_get_state
    """
    del ctx
    target_url = _validate_public_url(url)
    logger.info("tool web_fetch url=%s", target_url)
    try:
        final_url, content_type, text = await asyncio.to_thread(_request_url, target_url)
    except WebFetchError as exc:
        if exc.status_code in {401, 403, 429, 503}:
            return f"Fetch failed: {exc}\n\nHint: 网站可能存在反爬或需要验证（HTTP {exc.status_code}）。请改用 browser_navigate 等浏览器相关工具来访问此页面。"
        return f"Fetch failed: {exc}"

    text = _maybe_pretty_json(text)
    trimmed, truncated = trim_output(text, MAX_WEB_RESPONSE_CHARS)
    header = [
        f"URL: {final_url}",
        f"Content-Type: {content_type or 'unknown'}",
        f"Truncated: {truncated}",
        "-" * 40,
    ]
    return "\n".join(header) + "\n" + trimmed


async def discover_web_resources(ctx: RunContext[AgentDeps], url: str) -> str:
    """Discover RSS/Atom/JSON/API-like public resource links from a web page.

    Prefer discovered RSS, Atom, JSON, or documented API links before using the
    browser, unless the user explicitly requests browser automation.
    """
    del ctx
    target_url = _validate_public_url(url)
    logger.info("tool discover_web_resources url=%s", target_url)
    try:
        final_url, content_type, text = await asyncio.to_thread(_request_url, target_url)
    except WebFetchError as exc:
        if exc.status_code in {401, 403, 429, 503}:
            return f"Fetch failed: {exc}\n\nHint: 网站可能存在反爬或需要验证（HTTP {exc.status_code}）。请改用 browser_navigate 等浏览器相关工具来访问此页面。"
        return f"Fetch failed: {exc}"

    parser = ResourceLinkParser(final_url)
    parser.feed(text[:MAX_WEB_RESPONSE_CHARS])

    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for link in parser.links:
        link_url = link["url"]
        if link_url in seen:
            continue
        seen.add(link_url)
        links.append(link)
        if len(links) >= 30:
            break

    lines = [
        f"URL: {final_url}",
        f"Content-Type: {content_type or 'unknown'}",
        f"Title: {parser.title or 'unknown'}",
    ]
    if not links:
        lines.append("未发现明显的 RSS/Atom/JSON/API 链接。若确需网页交互，再使用 browser 工具。")
        return "\n".join(lines)

    lines.append("发现的候选公共资源：")
    for index, link in enumerate(links, start=1):
        meta = " ".join(item for item in (link.get("type", ""), link.get("rel", ""), link.get("title", "")) if item)
        lines.append(f"{index}. {link['url']}" + (f" ({meta})" if meta else ""))
    lines.append("建议：优先用 web_fetch 读取这些资源；只有公共资源不足时再打开浏览器。")
    return "\n".join(lines)
