"""长期记忆 embedding provider。"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from openhachimi_agent.core.config import MemoryEmbeddingConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    degraded: bool = False
    reason: str = ""


class EmbeddingProvider:
    def __init__(self, config: MemoryEmbeddingConfig) -> None:
        self.config = config

    def embed_sync(self, text: str) -> EmbeddingResult:
        if not self.config.enabled:
            logger.info("memory embedding skipped: embedding disabled")
            return EmbeddingResult(vector=[], degraded=True, reason="embedding_disabled")
        if not self.config.api_key:
            logger.warning("memory embedding skipped: missing api_key model=%s", self.config.model)
            return EmbeddingResult(vector=[], degraded=True, reason="missing_api_key")
        if not self.config.base_url:
            logger.warning("memory embedding skipped: missing base_url model=%s", self.config.model)
            return EmbeddingResult(vector=[], degraded=True, reason="missing_base_url")

        url = self.config.base_url.rstrip("/") + "/embeddings"
        payload = json.dumps({"model": self.config.model, "input": text}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            data = json.loads(raw.decode("utf-8"))
            vector = data["data"][0]["embedding"]
            if not isinstance(vector, list) or not vector:
                raise ValueError("embedding response has empty vector")
            vector = [float(value) for value in vector]
            logger.info(
                "memory embedding succeeded model=%s chars=%d dimensions=%d duration_ms=%d",
                self.config.model,
                len(text),
                len(vector),
                elapsed_ms,
            )
            return EmbeddingResult(vector=vector)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "memory embedding failed model=%s chars=%d duration_ms=%d error=%s",
                self.config.model,
                len(text),
                elapsed_ms,
                exc,
            )
            return EmbeddingResult(vector=[], degraded=True, reason=str(exc))

    async def embed(self, text: str) -> EmbeddingResult:
        return self.embed_sync(text)
