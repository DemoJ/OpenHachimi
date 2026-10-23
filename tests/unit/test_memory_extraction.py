from dataclasses import replace

from openhachimi_agent.memory.extraction import extract_memories_from_turn
from openhachimi_agent.memory.llm import MemoryExtractionOutput, MemoryLLMItem
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.privacy import PrivacyGuard


def test_extracts_preference_from_chinese_turn():
    scope = MemoryScope(role_name="default")

    result = extract_memories_from_turn("请记住：以后回答优先使用中文并保持简洁。", "", scope, "turn1")

    assert result.memories
    assert result.memories[0].memory_type == "user_preference"
    assert result.memories[0].stability == "stable"


def test_extracts_constraint_from_turn():
    scope = MemoryScope(role_name="default")

    result = extract_memories_from_turn("以后不要创建占位实现，必须真的落地。", "", scope, "turn1")

    assert result.memories[0].memory_type == "user_constraint"


def test_secret_is_rejected():
    scope = MemoryScope(role_name="default")
    guard = PrivacyGuard(allow_secret_memory=False)

    result = extract_memories_from_turn("记住我的 API key 是 sk-abcdefghijklmnopqrstuvwxyz", "", scope, "turn1", privacy_guard=guard)

    assert result.memories == []


def test_extracts_implicit_preference():
    scope = MemoryScope(role_name="default")

    result = extract_memories_from_turn("帮我把默认字体改成 14px", "", scope, "turn1")

    assert result.memories
    assert result.memories[0].memory_type == "user_preference"
    assert "implicit" in result.memories[0].tags


def test_llm_extraction_uses_agent_when_configured(mock_config, monkeypatch):
    scope = MemoryScope(role_name="default")
    config = replace(mock_config, openai_api_key="key", openai_base_url="https://llm.example/v1")
    captured = {}

    def fake_run_memory_extraction(config_arg, *, system_prompt, payload):
        captured["config"] = config_arg
        captured["system_prompt"] = system_prompt
        captured["payload"] = payload
        return MemoryExtractionOutput(
            memories=[
                MemoryLLMItem(
                    memory_type="workflow",
                    content="user prefers checklist workflow",
                    confidence=0.91,
                    stability="stable",
                )
            ]
        )

    monkeypatch.setattr("openhachimi_agent.memory.extraction.run_memory_extraction", fake_run_memory_extraction)

    result = extract_memories_from_turn("帮我按 checklist 做事", "assistant output", scope, "turn1", config=config)

    assert result.memories[0].memory_type == "workflow"
    assert captured["config"] is config
    assert "长期记忆" in captured["system_prompt"]
    assert captured["payload"] == {"user_message": "帮我按 checklist 做事", "assistant_output": "assistant output"}


def test_llm_extraction_degrades_to_rules_on_failure(mock_config, monkeypatch):
    scope = MemoryScope(role_name="default")
    config = replace(mock_config, openai_api_key="key", openai_base_url="https://llm.example/v1")

    def fake_run_memory_extraction(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("openhachimi_agent.memory.extraction.run_memory_extraction", fake_run_memory_extraction)

    result = extract_memories_from_turn("请记住：以后回答优先使用中文。", "", scope, "turn1", config=config)

    assert result.memories
    assert result.memories[0].memory_type == "user_preference"


def _llm_config(mock_config):
    return replace(mock_config, openai_api_key="key", openai_base_url="https://llm.example/v1")


def test_llm_empty_result_does_not_fall_back_to_rules(mock_config, monkeypatch):
    """LLM 判断这轮没有值得记的,就不再用关键词规则补抽(否则任务里的"不要改动"会被当成长期约束)。"""
    monkeypatch.setattr(
        "openhachimi_agent.memory.extraction.run_memory_extraction",
        lambda *args, **kwargs: MemoryExtractionOutput(memories=[]),
    )

    result = extract_memories_from_turn(
        "帮我看下这个函数，先不要改动任何文件", "", MemoryScope(role_name="default"), "turn1", config=_llm_config(mock_config)
    )

    assert result.memories == []


def test_llm_unavailable_falls_back_to_rules(mock_config, monkeypatch):
    monkeypatch.setattr("openhachimi_agent.memory.extraction.run_memory_extraction", lambda *args, **kwargs: None)

    result = extract_memories_from_turn(
        "以后不要创建占位实现，必须真的落地。", "", MemoryScope(role_name="default"), "turn1", config=_llm_config(mock_config)
    )

    assert result.memories and result.memories[0].memory_type == "user_constraint"


def test_llm_memories_get_expiry_from_stability(mock_config, monkeypatch):
    items = [
        MemoryLLMItem(memory_type="task_reference", content="整理 example.com 的文章", stability="ephemeral"),
        MemoryLLMItem(memory_type="project_fact", content="项目这周在迁移数据库", stability="situational"),
        MemoryLLMItem(memory_type="user_preference", content="使用中文回答", stability="stable"),
    ]
    monkeypatch.setattr(
        "openhachimi_agent.memory.extraction.run_memory_extraction",
        lambda *args, **kwargs: MemoryExtractionOutput(memories=items),
    )

    result = extract_memories_from_turn("随便一句话，足够长度", "", MemoryScope(role_name="default"), "turn1", config=_llm_config(mock_config))

    ephemeral, situational, stable = result.memories
    assert ephemeral.valid_until and ephemeral.decay_at
    assert situational.valid_until is None and situational.decay_at
    assert stable.valid_until is None and stable.decay_at is None


def test_llm_extraction_receives_recent_turns(mock_config, monkeypatch):
    captured = {}

    def fake(config_arg, *, system_prompt, payload):
        captured.update(payload)
        return MemoryExtractionOutput(memories=[])

    monkeypatch.setattr("openhachimi_agent.memory.extraction.run_memory_extraction", fake)
    recent = [{"user_message": "要不要用 PostgreSQL?", "assistant_output": "建议用 PostgreSQL"}]

    extract_memories_from_turn(
        "好的，就按你说的定下来", "", MemoryScope(role_name="default"), "turn2", config=_llm_config(mock_config), recent_turns=recent
    )

    assert captured["recent_turns"] == recent


def test_llm_unusable_items_fall_back_to_rules(mock_config, monkeypatch):
    """模型给了条目但 content 全是空的(真实模型出现过),按失败处理,退回规则抽取。"""
    monkeypatch.setattr(
        "openhachimi_agent.memory.extraction.run_memory_extraction",
        lambda *args, **kwargs: MemoryExtractionOutput(memories=[MemoryLLMItem(memory_type="user_preference", content="", tags=["中文"])]),
    )

    result = extract_memories_from_turn(
        "请记住：以后回答我都用中文，并且尽量简洁。", "", MemoryScope(role_name="default"), "turn1", config=_llm_config(mock_config)
    )

    assert [m.content for m in result.memories] == ["以后回答我都用中文"]
