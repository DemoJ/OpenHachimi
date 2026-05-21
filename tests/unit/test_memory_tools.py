import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "memory_tools_module",
    Path(__file__).resolve().parents[2] / "openhachimi_agent" / "tools" / "memory.py",
)
memory_tools = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(memory_tools)
_normalize_tags = memory_tools._normalize_tags


def test_normalize_tags_accepts_json_string():
    assert _normalize_tags('["location", "timezone", "user-preference"]') == [
        "location",
        "timezone",
        "user-preference",
    ]


def test_normalize_tags_accepts_comma_string():
    assert _normalize_tags("language, user-preference") == ["language", "user-preference"]
