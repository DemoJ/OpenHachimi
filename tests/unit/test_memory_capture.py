from openhachimi_agent.memory.capture import capture_turn_memories
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.recall import recall_memories


def test_capture_turn_writes_l0_and_extracts_explicit_preference(mock_config):
    scope = MemoryScope(role_name="default", session_id="s1")

    turn_id = capture_turn_memories(mock_config, scope, "remember: prefer concise Chinese answers", "ok")
    context = recall_memories(mock_config, scope, "Chinese answers")

    assert turn_id
    assert context.results
    assert any("Chinese" in item.content for item in context.results)
