from openhachimi_agent.memory.formatting import format_memory_context
from openhachimi_agent.memory.models import MemoryContext, MemoryScope, MemorySearchResult
from openhachimi_agent.memory.recall import get_memory_store


def test_format_memory_context_includes_xml_tags():
    context = MemoryContext(
        scope=MemoryScope(),
        query="中文",
        results=[
            MemorySearchResult(
                id="m1",
                level="L1",
                content="用户偏好中文。",
                score=1.0,
                memory_type="preference",
                confidence=0.9,
                updated_at="now",
                source="bm25",
                metadata={"stability": "stable", "access_count": 3, "created_at": "then"},
            )
        ],
    )

    text = format_memory_context(context)

    assert "<memory-context>" in text
    assert "用户偏好中文" in text
    assert "source=\"bm25\"" in text
    assert "stability=\"stable\"" in text
    assert "access_count=\"3\"" in text
    assert "created_at=\"then\"" in text
    assert "</memory-context>" in text


def test_get_memory_store_reuses_instance_for_same_db_path(mock_config):
    first = get_memory_store(mock_config)
    second = get_memory_store(mock_config)

    assert first is second
