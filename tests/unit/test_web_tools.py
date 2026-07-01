# pyrefly: ignore [missing-import]
import importlib.util
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_TOOLS_DIR = Path(__file__).parents[2] / "openhachimi_agent" / "tools"
_tools_pkg = types.ModuleType("openhachimi_agent.tools")
_tools_pkg.__path__ = [str(_TOOLS_DIR)]
sys.modules["openhachimi_agent.tools"] = _tools_pkg

_web_spec = importlib.util.spec_from_file_location(
    "openhachimi_agent.tools.web",
    _TOOLS_DIR / "web.py",
)
web_module = importlib.util.module_from_spec(_web_spec)
assert _web_spec.loader is not None
_web_spec.loader.exec_module(web_module)

# web._validate_public_host 转调 url_security.assert_public_hostname，
# DNS 解析（getaddrinfo）实际发生在 url_security 模块内，故 patch 目标是它。
url_security_module = sys.modules["openhachimi_agent.tools.url_security"]


@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        (
            "https://zh.wikipedia.org/wiki/监狱来的妈妈",
            "https://zh.wikipedia.org/wiki/%E7%9B%91%E7%8B%B1%E6%9D%A5%E7%9A%84%E5%A6%88%E5%A6%88",
        ),
        (
            "https://example.com/search?q=监狱来的妈妈&lang=zh#章节 1",
            "https://example.com/search?q=%E7%9B%91%E7%8B%B1%E6%9D%A5%E7%9A%84%E5%A6%88%E5%A6%88&lang=zh#%E7%AB%A0%E8%8A%82%201",
        ),
        (
            "https://example.com/wiki/%E7%9B%91%E7%8B%B1%E6%9D%A5%E7%9A%84%E5%A6%88%E5%A6%88",
            "https://example.com/wiki/%E7%9B%91%E7%8B%B1%E6%9D%A5%E7%9A%84%E5%A6%88%E5%A6%88",
        ),
        (
            "例子.测试/路径?q=值",
            "https://xn--fsqu00a.xn--0zwm56d/%E8%B7%AF%E5%BE%84?q=%E5%80%BC",
        ),
        (
            "HTTP://Example.COM:8080/a",
            "http://example.com:8080/a",
        ),
    ],
)
def test_normalize_public_url_returns_ascii_url(input_url, expected):
    result = web_module._normalize_public_url(input_url)

    assert result == expected
    result.encode("ascii")


@pytest.mark.parametrize(
    "input_url",
    [
        "",
        "ftp://example.com/file",
        "https://",
        "https://example.com/a\tb",
        "https://example.com/a\nSet-Cookie:bad",
        "https://user@example.com/path",
        "https://user:pass@example.com/path",
        "https://example.com:99999/path",
        "https://example.com/%E7%9B%91%FF",
    ],
)
def test_normalize_public_url_rejects_invalid_urls(input_url):
    with pytest.raises(ValueError):
        web_module._normalize_public_url(input_url)


@pytest.mark.asyncio
async def test_web_fetch_requests_normalized_ascii_url(monkeypatch):
    captured = {}

    def fake_request_url(url):
        captured["url"] = url
        return url, "text/plain; charset=utf-8", "ok"

    async def fake_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(web_module, "_request_url", fake_request_url)
    monkeypatch.setattr(web_module.asyncio, "to_thread", fake_to_thread)

    result = await web_module.web_fetch(SimpleNamespace(), "https://zh.wikipedia.org/wiki/监狱来的妈妈")

    assert captured["url"] == "https://zh.wikipedia.org/wiki/%E7%9B%91%E7%8B%B1%E6%9D%A5%E7%9A%84%E5%A6%88%E5%A6%88"
    captured["url"].encode("ascii")
    assert "ok" in result


@pytest.mark.asyncio
async def test_web_fetch_antibot_status_suggests_browser_extract_content(monkeypatch):
    def fake_request_url(url):
        raise web_module.WebFetchError("HTTP 403 Forbidden", status_code=403)

    async def fake_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(web_module, "_request_url", fake_request_url)
    monkeypatch.setattr(web_module.asyncio, "to_thread", fake_to_thread)

    result = await web_module.web_fetch(SimpleNamespace(), "https://example.com/protected")

    assert "Fetch failed" in result
    assert "browser_navigate" in result
    assert "browser_extract_content" in result
    assert "不要绕过" in result


@pytest.mark.asyncio
async def test_web_fetch_pretty_prints_json(monkeypatch):
    def fake_request_url(url):
        return url, "application/json; charset=utf-8", '{"message":"中文","ok":true}'

    async def fake_to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(web_module, "_request_url", fake_request_url)
    monkeypatch.setattr(web_module.asyncio, "to_thread", fake_to_thread)

    result = await web_module.web_fetch(SimpleNamespace(), "https://example.com/api")

    assert '"message": "中文"' in result
    assert '"ok": true' in result


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://[::1]/",
        "http://[2001:db8::1]:8080/路径",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
    ],
)
def test_normalize_public_url_rejects_non_public_hosts(blocked_url):
    with pytest.raises(ValueError):
        web_module._normalize_public_url(blocked_url)


def test_request_url_does_not_echo_http_error_body(monkeypatch):
    class FakeHTTPError(web_module.HTTPError):
        def read(self, *args, **kwargs):
            return b"secret-token-should-not-leak"

    def fake_open(request, timeout):
        raise FakeHTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(web_module, "_validate_public_host", lambda hostname, resolve_dns=False: None)
    monkeypatch.setattr(web_module._NO_REDIRECT_OPENER, "open", fake_open)

    with pytest.raises(web_module.WebFetchError) as exc_info:
        web_module._request_url("https://example.com/protected")

    assert "HTTP 403 Forbidden" in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)


def test_validate_public_host_allows_host_with_any_public_address(monkeypatch):
    """域名同时解析出公网 IPv4 和 Teredo 伪 IPv6 时应放行（修复 www.reuters.com 误杀）。"""

    def fake_getaddrinfo(host, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("174.37.54.20", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001::a88f:abba", 0, 0, 0)),
        ]

    monkeypatch.setattr(url_security_module.socket, "getaddrinfo", fake_getaddrinfo)

    # 不应抛出异常
    web_module._validate_public_host("www.reuters.com", resolve_dns=True)


def test_validate_public_host_rejects_host_resolving_only_to_private(monkeypatch):
    """域名所有解析结果均为非公网地址时仍应拒绝（保留 SSRF 基础防护）。"""

    def fake_getaddrinfo(host, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 0, 0, 0)),
        ]

    monkeypatch.setattr(url_security_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError):
        web_module._validate_public_host("intranet.example", resolve_dns=True)


def test_validate_public_host_rejects_mixed_public_and_private_addresses(monkeypatch):
    """公网地址旁挂私网地址时必须拒绝，避免 DNS rebinding/SSRF 绕过。"""

    def fake_getaddrinfo(host, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(url_security_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError):
        web_module._validate_public_host("mixed.example", resolve_dns=True)


@pytest.mark.asyncio
async def test_web_fetch_returns_error_string_for_blocked_host():
    """SSRF 校验失败应作为结果字符串返回，不应抛异常中断 agent run。"""
    result = await web_module.web_fetch(SimpleNamespace(), "http://10.0.0.1/")

    assert "Fetch failed" in result
    assert "非公网" in result


@pytest.mark.asyncio
async def test_discover_web_resources_returns_error_string_for_blocked_host():
    """discover_web_resources 遇到 SSRF 拦截同样应返回字符串而非抛异常。"""
    result = await web_module.discover_web_resources(SimpleNamespace(), "http://192.168.1.1/")

    assert "Fetch failed" in result
    assert "非公网" in result
