# pyrefly: ignore [missing-import]
import importlib.util
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

    monkeypatch.setattr(web_module._NO_REDIRECT_OPENER, "open", fake_open)

    with pytest.raises(web_module.WebFetchError) as exc_info:
        web_module._request_url("https://example.com/protected")

    assert "HTTP 403 Forbidden" in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)
