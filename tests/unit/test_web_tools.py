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
        (
            "http://[2001:db8::1]:8080/路径",
            "http://[2001:db8::1]:8080/%E8%B7%AF%E5%BE%84",
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
