import asyncio

from openhachimi_agent.core.config import MemoryEmbeddingConfig
from openhachimi_agent.memory.embeddings import EmbeddingProvider


def test_embedding_provider_reports_missing_api_key(caplog):
    provider = EmbeddingProvider(MemoryEmbeddingConfig(enabled=True, api_key=None, base_url="https://example.test/v1"))

    result = provider.embed_sync("hello")

    assert result.degraded is True
    assert result.reason == "missing_api_key"
    assert "memory embedding skipped: missing api_key" in caplog.text


def test_embedding_provider_async_uses_thread(monkeypatch):
    provider = EmbeddingProvider(MemoryEmbeddingConfig(enabled=False))
    calls = []

    async def fake_to_thread(func, *args):
        calls.append((func, args))
        return func(*args)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    result = asyncio.run(provider.embed("hello"))

    assert result.degraded is True
    assert calls == [(provider.embed_sync, ("hello",))]
