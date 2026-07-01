# pyrefly: ignore [missing-import]
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from openhachimi_agent.core.config import AppConfig, MemoryConfig, ResearchConfig, SchedulerConfig, VisionConfig

_TOOLS_DIR = Path(__file__).parents[2] / "openhachimi_agent" / "tools"
_tools_pkg = types.ModuleType("openhachimi_agent.tools")
_tools_pkg.__path__ = [str(_TOOLS_DIR)]
sys.modules["openhachimi_agent.tools"] = _tools_pkg

_research_spec = importlib.util.spec_from_file_location(
    "openhachimi_agent.tools.research",
    _TOOLS_DIR / "research.py",
)
research_module = importlib.util.module_from_spec(_research_spec)
assert _research_spec.loader is not None
sys.modules["openhachimi_agent.tools.research"] = research_module
_research_spec.loader.exec_module(research_module)


def _ctx(tmp_path, research_config=None):
    config = AppConfig(
        base_dir=tmp_path,
        user_dir=tmp_path / "user",
        config_path=tmp_path / "user" / "config.yaml",
        roles_dir=tmp_path / "roles",
        memory_dir=tmp_path / ".memory",
        model_name="test-model",
        openai_base_url="http://test",
        default_role_name="default",
        openai_api_key="test-key",
        llm_supports_vision="auto",
        log_dir=tmp_path / ".logs",
        log_level="INFO",
        log_console=False,
        skills_dirs=[tmp_path / "skills"],
        browser_headless=True,
        browser_channel=None,
        browser_user_agent=None,
        browser_window_size=None,
        browser_idle_timeout=300,
        browser_cdp_wait_seconds=45,
        telegram_bot_token=None,
        telegram_proxy_url=None,
        show_tool_calls=True,
        attachments_dir=tmp_path / ".tmp" / "attachments",
        max_attachment_size_bytes=50 * 1024 * 1024,
        allowed_attachment_mime_types=[],
        agent_timeout_seconds=300,
        stream_idle_timeout_seconds=60,
        memory=MemoryConfig(db_path=tmp_path / ".memory" / "memory.sqlite3"),
        scheduler=SchedulerConfig(db_path=tmp_path / ".scheduler" / "tasks.sqlite3"),
        research=research_config or ResearchConfig(),
        vision=VisionConfig(api_key="test-key", base_url="http://test"),
        http_api_token="test-token",
    )
    return SimpleNamespace(deps=SimpleNamespace(config=config))


def test_clean_query_rejects_blank_query():
    with pytest.raises(ValueError):
        research_module._clean_query("   ")


def test_clean_external_text_truncates_and_strips_control_chars():
    text = research_module._clean_external_text("hello\x00\x01 " + "x" * 300, 20)

    assert "\x00" not in text
    assert "\x01" not in text
    assert len(text) <= 23
    assert text.endswith("...")


def test_canonicalize_result_url_drops_tracking_params():
    result = research_module._canonicalize_result_url(
        "HTTPS://Example.COM/path/?utm_source=x&a=1&fbclid=bad#section"
    )

    assert result == "https://example.com/path?a=1"


def test_canonicalize_result_url_normalizes_www_and_default_port():
    assert research_module._canonicalize_result_url("https://www.example.com:443/a/") == "https://example.com/a"
    assert research_module._canonicalize_result_url("http://example.com:80/a") == "http://example.com/a"


def test_rank_sources_skips_malformed_result_urls():
    results = [
        research_module.SearchResult("Bad", "https://example.com:bad/path", "broken", "duckduckgo", 1),
        research_module.SearchResult("Good", "https://example.com/good", "useful", "duckduckgo", 2),
    ]

    ranked = research_module._rank_sources(results)

    assert len(ranked) == 1
    assert ranked[0].title == "Good"


def test_enabled_backend_without_api_key_reports_error(tmp_path):
    backend, results, error = asyncio_run(
        research_module._search_backend(
            "brave",
            "topic",
            3,
            ResearchConfig(enabled_backends=["brave"]),
        )
    )

    assert backend == "brave"
    assert results is None
    assert "brave_api_key" in error


def test_request_json_rejects_non_public_host():
    with pytest.raises(ValueError):
        research_module._request_json("http://127.0.0.1/search", {}, 1)


def test_request_json_uses_no_redirect_opener_and_parses_json(monkeypatch):
    captured = {}

    class FakeResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def geturl(self):
            return "https://api.search.brave.com/res/v1/web/search?q=test"

        def read(self, limit):
            captured["limit"] = limit
            return b'{"ok": true}'

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(research_module, "_validate_public_host", lambda hostname, resolve_dns=False: None)
    monkeypatch.setattr(research_module._NO_REDIRECT_OPENER, "open", fake_open)

    payload = research_module._request_json(
        "https://api.search.brave.com/res/v1/web/search?q=test",
        {"Accept": "application/json"},
        7,
    )

    assert payload == {"ok": True}
    assert captured["url"] == "https://api.search.brave.com/res/v1/web/search?q=test"
    assert captured["timeout"] == 7
    assert captured["limit"] == 2_000_000


def test_search_tavily_posts_through_safe_json_request(monkeypatch):
    captured = {}

    def fake_request_json(url, headers, timeout, data=None, method=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["data"] = data
        captured["method"] = method
        return {
            "results": [
                {
                    "title": "Result",
                    "url": "https://example.com/article",
                    "content": "Snippet",
                }
            ]
        }

    monkeypatch.setattr(research_module, "_request_json", fake_request_json)

    results = research_module._search_tavily(
        "topic",
        3,
        ResearchConfig(tavily_api_key="key", search_timeout_seconds=9),
    )

    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["Authorization"] == "Bearer key"
    assert captured["timeout"] == 9
    assert captured["method"] == "POST"
    body = research_module.json.loads(captured["data"].decode("utf-8"))
    assert body["query"] == "topic"
    # 安全：API key 必须走 Authorization header，不得出现在请求体中，
    # 避免代理/APM 等记录 body 的系统泄露密钥。
    assert "api_key" not in body
    assert "key" not in captured["data"].decode("utf-8")
    assert results[0].backend == "tavily"
    assert results[0].url == "https://example.com/article"


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def test_rank_sources_merges_duplicate_urls():
    results = [
        research_module.SearchResult("Docs", "https://docs.example.com/a?utm_source=x", "official docs", "duckduckgo", 1),
        research_module.SearchResult("Docs mirror", "https://docs.example.com/a", "same", "brave", 2),
        research_module.SearchResult("Coupon", "https://coupon.example.com", "free download coupon", "duckduckgo", 1),
    ]

    ranked = research_module._rank_sources(results)

    # 去重:utm_source 被剥离后两条 docs URL 合并为一条,backends 含两个后端
    assert ranked[0].url.startswith("https://docs.example.com/a")
    assert set(ranked[0].backends) == {"duckduckgo", "brave"}


@pytest.mark.asyncio
async def test_web_search_formats_results_and_clamps_max_results(monkeypatch, tmp_path):
    captured = {}

    async def fake_search_all(query, max_results, config):
        captured["query"] = query
        captured["max_results"] = max_results
        return research_module.SearchRunResult(
            query=query,
            results=[research_module.SearchResult("Title", "https://example.com", "Snippet", "duckduckgo", 1)],
            backend_errors={},
            attempted_backends=["duckduckgo"],
        )

    monkeypatch.setattr(research_module, "_search_all_backends", fake_search_all)

    output = await research_module.web_search(_ctx(tmp_path), "hello", max_results=999)

    assert captured["max_results"] == 50
    assert "搜索 'hello' 共返回 1 条去重结果" in output
    assert "Title" in output
    # 原子搜索:不再输出引用编号/工作流包装
    assert "[S1]" not in output
    assert "Citation requirement" not in output
