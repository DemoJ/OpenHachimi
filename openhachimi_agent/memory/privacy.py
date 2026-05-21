"""长期记忆隐私与敏感信息处理。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from openhachimi_agent.memory.models import MemorySensitivity


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(session|cookie)\s*[:=]\s*[^\s;]{12,}"),
]

_PII_PATTERNS = [
    re.compile(r"\b[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
]


@dataclass(frozen=True)
class PrivacyDecision:
    action: str
    sensitivity: MemorySensitivity
    text: str
    reason: str = ""


class PrivacyGuard:
    def __init__(self, allow_secret_memory: bool = False, pii_redaction: bool = True) -> None:
        self.allow_secret_memory = allow_secret_memory
        self.pii_redaction = pii_redaction

    def classify(self, text: str) -> MemorySensitivity:
        if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
            return MemorySensitivity.SECRET
        if any(pattern.search(text) for pattern in _PII_PATTERNS):
            return MemorySensitivity.SENSITIVE
        return MemorySensitivity.PERSONAL

    def redact(self, text: str) -> str:
        redacted = text
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
        for pattern in _PII_PATTERNS:
            redacted = pattern.sub("[REDACTED_PII]", redacted)
        return redacted

    def should_store(self, text: str) -> PrivacyDecision:
        sensitivity = self.classify(text)
        if sensitivity == MemorySensitivity.SECRET and not self.allow_secret_memory:
            return PrivacyDecision("reject", sensitivity, "", "secret_detected")
        if sensitivity == MemorySensitivity.SENSITIVE and self.pii_redaction:
            return PrivacyDecision("redact", sensitivity, self.redact(text), "pii_redacted")
        return PrivacyDecision("allow", sensitivity, text)
