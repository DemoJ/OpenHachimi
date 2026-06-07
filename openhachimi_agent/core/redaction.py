"""Shared redaction helpers for user-visible errors and tool output."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "passwd",
    "password",
    "private_key",
    "secret",
    "token",
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|cookie)(\s*[:=]\s*)([^\s'\";&]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except Exception:
        return url
    netloc = parsed.netloc
    if "@" in netloc:
        host = netloc.rsplit("@", 1)[-1]
        netloc = f"{REDACTED}@{host}"
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        query_items.append((key, REDACTED if is_sensitive_key(key) else value))
    query = urlencode(query_items, doseq=True)
    fragment = REDACTED if parsed.fragment and is_sensitive_key("fragment") else parsed.fragment
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))


def redact_text(text: object, max_chars: int | None = None) -> str:
    redacted = str(text)
    redacted = _URL_RE.sub(lambda match: redact_url(match.group(0)), redacted)
    for pattern in SENSITIVE_VALUE_PATTERNS:
        if pattern.pattern.startswith("(?i)\\b(Bearer"):
            redacted = pattern.sub(r"\1" + REDACTED, redacted)
        elif "api" in pattern.pattern and "authorization" in pattern.pattern:
            redacted = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", redacted)
        else:
            redacted = pattern.sub(REDACTED, redacted)
    if max_chars is not None and len(redacted) > max_chars:
        return redacted[: max_chars - 3] + "..."
    return redacted


def redact_tool_args(args: object) -> object:
    if isinstance(args, dict):
        return {key: REDACTED if is_sensitive_key(key) else redact_tool_args(value) for key, value in args.items()}
    if isinstance(args, list):
        return [redact_tool_args(item) for item in args]
    if isinstance(args, tuple):
        return tuple(redact_tool_args(item) for item in args)
    if isinstance(args, str):
        return redact_text(args)
    return args


def redact_exception(exc: BaseException, *, include_type: bool = True, max_chars: int = 1000) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    redacted = redact_text(text, max_chars=max_chars)
    if include_type:
        return f"{exc.__class__.__name__}: {redacted}"
    return redacted


def safe_error_detail(exc: BaseException | str, *, max_chars: int = 1000) -> str:
    if isinstance(exc, BaseException):
        return redact_exception(exc, include_type=True, max_chars=max_chars)
    return redact_text(exc, max_chars=max_chars)


def summarize_redacted(value: object, max_chars: int = 160) -> str:
    value = redact_tool_args(value)
    if value in (None, "", {}):
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except TypeError:
            text = str(value)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text
