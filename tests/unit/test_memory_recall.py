from openhachimi_agent.memory.formatting import format_memory_context
from openhachimi_agent.memory.models import MemoryContext, MemoryScope, MemorySearchResult


def test_format_memory_context_includes_xml_tags():
    context = MemoryContext(
        scope=MemoryScope(),
        query="中文",
        results=[MemorySearchResult(id="m1", level="L1", content="用户偏好中文。", score=1.0, memory_type="preference", confidence=0.9, updated_at="now")],
    )

    text = format_memory_context(context)

    assert "<memory-context>" in text
    assert "用户偏好中文" in text
    assert "</memory-context>" in text
