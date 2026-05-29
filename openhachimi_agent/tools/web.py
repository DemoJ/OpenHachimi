"""Lightweight public web resource tools.

These tools are intentionally simpler than the browser tools. They are useful
for public HTML, JSON, RSS, Atom, and documented API endpoints, and should be
tried before opening a browser unless the user explicitly asks for browser use.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic_ai import RunContext

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.tools.utils import trim_output


logger = logging.getLogger(__name__)

MAX_WEB_RESPONSE_CHARS = 60000
WEB_TIMEOUT_SECONDS = 20
WEB_USER_AGENT = "OpenHachimi-Agent/0.1 (+https://github.com/DemoJ/OpenHachimi)"
PATH_SAFE_CHARS = "/:@!$&'()*+,;="
QUERY_SAFE_CHARS = "/?:@!$&'()*+,;="
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class WebFetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(NoRedirectHandler)


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


def _is_blocked_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def _validate_public_host(hostname: str, resolve_dns: bool = False) -> None:
    lowered = hostname.strip().lower().rstrip(".")
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost") or lowered.endswith(".local"):
        raise ValueError(f"URL 主机不允许访问本机或局域网地址：{hostname}")

    try:
        if _is_blocked_ip(lowered.strip("[]")):
            raise ValueError(f"URL 主机不允许访问非公网地址：{hostname}")
        return
    except ValueError as exc:
        if "不允许" in str(exc):
            raise

    if not resolve_dns:
        return

    try:
        resolved = socket.getaddrinfo(lowered, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"URL 主机名无法解析：{hostname}") from exc
    for family, _, _, _, sockaddr in resolved:
        address = sockaddr[0]
        if _is_blocked_ip(address):
            raise ValueError(f"URL 主机解析到非公网地址：{hostname} -> {address}")


def _quote_url_component(value: str, safe: str) -> str:
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"URL 包含无效的百分号编码：{value}") from exc
    return quote(decoded, safe=safe)


def _normalize_host(hostname: str) -> str:
    try:
        ipaddress.IPv6Address(hostname)
    except ValueError:
        pass
    else:
        return f"[{hostname.lower()}]"

    try:
        return hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"URL 主机名无效：{hostname}") from exc


def _normalize_public_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("url 不能为空")
    if CONTROL_CHAR_RE.search(url):
        raise ValueError(f"URL 包含非法控制字符：{url}")
    if "://" not in url:
        url = "https://" + url

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError(f"仅支持 http/https URL：{url}")
    if parsed.username or parsed.password:
        raise ValueError("URL 不能包含用户名或密码")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"URL 端口无效：{url}") from exc

    netloc = _normalize_host(parsed.hostname)
    _validate_public_host(netloc.strip("[]"), resolve_dns=False)
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = _quote_url_component(parsed.path, PATH_SAFE_CHARS)
    query = _quote_url_component(parsed.query, QUERY_SAFE_CHARS)
    fragment = _quote_url_component(parsed.fragment, QUERY_SAFE_CHARS)
    return urlunsplit((scheme, netloc, path, query, fragment))


def _request_url(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    if parsed.hostname:
        _validate_public_host(parsed.hostname, resolve_dns=True)
    request = Request(url, headers={"User-Agent": WEB_USER_AGENT, "Accept": "*/*"})
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=WEB_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("content-type", "")
            final_url = _normalize_public_url(response.geturl())
            raw = response.read(MAX_WEB_RESPONSE_CHARS + 1)
    except HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            location = exc.headers.get("Location")
            if location:
                redirect_url = _normalize_public_url(urljoin(url, location))
                raise WebFetchError(f"HTTP {exc.code} 重定向到 {redirect_url}；为防止 SSRF，web_fetch 不自动跟随重定向。请确认目标为公开网页后直接访问重定向 URL。", status_code=exc.code) from exc
        raise WebFetchError(f"HTTP {exc.code} {exc.reason}", status_code=exc.code) from exc
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
    如果返回 'Fetch failed' + 'Hint'，说明该页面可能有反爬保护，请改用 browser_navigate 后再调用 browser_extract_content。

    【工具选择建议】：
    - 先用 research_sources 获取带引用编号的高质量来源，再用本工具读取页面内容
    - 轻量查找可先用 web_search 获取相关链接，再用本工具读取页面内容
    - 若本工具失败（403 / 429 等），升级到 browser_navigate(url) + browser_extract_content()
    """
    del ctx
    target_url = _normalize_public_url(url)
    logger.info("tool web_fetch url=%s", target_url)
    try:
        final_url, content_type, text = await asyncio.to_thread(_request_url, target_url)
    except WebFetchError as exc:
        if exc.status_code in {401, 403, 429, 503}:
            return f"Fetch failed: {exc}\n\nHint: 网站可能存在反爬或需要验证（HTTP {exc.status_code}）。请先尝试 discover_web_resources(url) 寻找公开 RSS/API/JSON；若仍需渲染公开页面，请调用 browser_navigate(url)，再调用 browser_extract_content() 读取正文。遇到 CAPTCHA/登录墙/付费墙时不要绕过，应换公开来源或说明信息不足。"
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
    target_url = _normalize_public_url(url)
    logger.info("tool discover_web_resources url=%s", target_url)
    try:
        final_url, content_type, text = await asyncio.to_thread(_request_url, target_url)
    except WebFetchError as exc:
        if exc.status_code in {401, 403, 429, 503}:
            return f"Fetch failed: {exc}\n\nHint: 网站可能存在反爬或需要验证（HTTP {exc.status_code}）。请先尝试 discover_web_resources(url) 寻找公开 RSS/API/JSON；若仍需渲染公开页面，请调用 browser_navigate(url)，再调用 browser_extract_content() 读取正文。遇到 CAPTCHA/登录墙/付费墙时不要绕过，应换公开来源或说明信息不足。"
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
