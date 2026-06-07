"""Public URL normalization and SSRF guard helpers."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import quote, unquote, urlsplit, urlunsplit

PATH_SAFE_CHARS = "/:@!$&'()*+,;="
QUERY_SAFE_CHARS = "/?:@!$&'()*+,;="
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class PublicUrlError(ValueError):
    """Raised when a URL is not safe to access as a public web resource."""


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


def _is_teredo_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return isinstance(ip, ipaddress.IPv6Address) and ip.teredo is not None


def assert_public_hostname(hostname: str, *, resolve_dns: bool = False) -> None:
    lowered = hostname.strip().lower().rstrip(".")
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost") or lowered.endswith(".local"):
        raise PublicUrlError(f"URL 主机不允许访问本机或局域网地址：{hostname}")

    try:
        if _is_blocked_ip(lowered.strip("[]")):
            raise PublicUrlError(f"URL 主机不允许访问非公网地址：{hostname}")
        return
    except ValueError as exc:
        if isinstance(exc, PublicUrlError):
            raise

    if not resolve_dns:
        return

    try:
        resolved = socket.getaddrinfo(lowered, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PublicUrlError(f"URL 主机名无法解析：{hostname}") from exc

    addresses = [sockaddr[0] for *_unused, sockaddr in resolved]
    blocked_addresses = [address for address in addresses if _is_blocked_ip(address)]
    dangerous_addresses = [address for address in blocked_addresses if not _is_teredo_ip(address)]
    if dangerous_addresses:
        raise PublicUrlError(f"URL 主机解析到非公网地址：{hostname} -> {dangerous_addresses[0]}")
    if addresses and len(blocked_addresses) == len(addresses):
        raise PublicUrlError(f"URL 主机解析到非公网地址：{hostname} -> {addresses[0]}")


def _quote_url_component(value: str, safe: str) -> str:
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeDecodeError as exc:
        raise PublicUrlError(f"URL 包含无效的百分号编码：{value}") from exc
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
        raise PublicUrlError(f"URL 主机名无效：{hostname}") from exc


def normalize_public_http_url(url: str, *, resolve_dns: bool = False) -> str:
    url = url.strip()
    if not url:
        raise PublicUrlError("url 不能为空")
    if CONTROL_CHAR_RE.search(url):
        raise PublicUrlError(f"URL 包含非法控制字符：{url}")
    if "://" not in url:
        url = "https://" + url

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise PublicUrlError(f"仅支持 http/https URL：{url}")
    if parsed.username or parsed.password:
        raise PublicUrlError("URL 不能包含用户名或密码")

    try:
        port = parsed.port
    except ValueError as exc:
        raise PublicUrlError(f"URL 端口无效：{url}") from exc

    netloc = _normalize_host(parsed.hostname)
    assert_public_hostname(netloc.strip("[]"), resolve_dns=resolve_dns)
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = _quote_url_component(parsed.path, PATH_SAFE_CHARS)
    query = _quote_url_component(parsed.query, QUERY_SAFE_CHARS)
    fragment = _quote_url_component(parsed.fragment, QUERY_SAFE_CHARS)
    return urlunsplit((scheme, netloc, path, query, fragment))


def validate_public_http_url(url: str, *, resolve_dns: bool = True) -> str:
    return normalize_public_http_url(url, resolve_dns=resolve_dns)
