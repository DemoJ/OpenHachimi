from dataclasses import replace

from openhachimi_agent.memory.extraction import extract_memories_from_turn
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.privacy import PrivacyGuard


def test_extracts_preference_from_chinese_turn():
    scope = MemoryScope(role_name="default")

    result = extract_memories_from_turn("请记住：以后回答优先使用中文并保持简洁。", "", scope, "turn1")

    assert result.memories
    assert result.memories[0].memory_type == "preference"
    assert result.memories[0].stability == "stable"


def test_extracts_constraint_from_turn():
    scope = MemoryScope(role_name="default")

    result = extract_memories_from_turn("以后不要创建占位实现，必须真的落地。", "", scope, "turn1")

    assert result.memories[0].memory_type == "constraint"


def test_secret_is_rejected():
    scope = MemoryScope(role_name="default")
    guard = PrivacyGuard(allow_secret_memory=False)

    result = extract_memories_from_turn("记住我的 API key 是 sk-abcdefghijklmnopqrstuvwxyz", "", scope, "turn1", privacy_guard=guard)

    assert result.memories == []


def test_extracts_implicit_preference():
    scope = MemoryScope(role_name="default")

    result = extract_memories_from_turn("帮我把默认字体改成 14px", "", scope, "turn1")

    assert result.memories
    assert result.memories[0].memory_type == "preference"
    assert "implicit" in result.memories[0].tags


def test_llm_extraction_uses_prompt_when_configured(mock_config, monkeypatch):
    scope = MemoryScope(role_name="default")
    config = replace(mock_config, openai_api_key="key", openai_base_url="https://llm.example/v1")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"memories\\":[{\\"memory_type\\":\\"workflow\\",\\"content\\":\\"user prefers checklist workflow\\",\\"confidence\\":0.91,\\"stability\\":\\"stable\\"}]}"}}]}'

    def fake_urlopen(request, timeout):
        captured["body"] = request.data.decode("utf-8")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = extract_memories_from_turn("帮我按 checklist 做事", "", scope, "turn1", config=config)

    assert result.memories[0].memory_type == "workflow"
    assert "长期记忆" in captured["body"]
