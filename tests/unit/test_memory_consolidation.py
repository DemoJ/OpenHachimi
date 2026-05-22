from dataclasses import replace

from openhachimi_agent.memory.consolidation import consolidate_due_memories
from openhachimi_agent.memory.models import MemoryAtom, MemoryScope, MemoryStability
from openhachimi_agent.memory.store import MemoryStore


def test_consolidates_related_atoms_into_block(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    store.add_atom(MemoryAtom(memory_type="preference", content="用户偏好中文回答", scope=scope, keywords=["中文"], tags=["回答"], stability=MemoryStability.STABLE))
    store.add_atom(MemoryAtom(memory_type="preference", content="用户要求中文解释代码", scope=scope, keywords=["中文"], tags=["回答"], stability=MemoryStability.STABLE))

    result = consolidate_due_memories(store, scope=scope)
    results = store.search(scope, "中文 回答", limit=10)

    assert result["blocks_created"] >= 1
    assert any(item.level == "L2" for item in results)


def test_consolidates_stable_preferences_into_profile(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    store.add_atom(MemoryAtom(memory_type="preference", content="用户偏好简洁回答", scope=scope, keywords=["简洁"], tags=["回答"], stability=MemoryStability.STABLE))
    store.add_atom(MemoryAtom(memory_type="constraint", content="用户要求不要写占位符", scope=scope, keywords=["占位符"], tags=["实现"], stability=MemoryStability.STABLE))

    result = consolidate_due_memories(store, scope=scope)

    assert result["profiles_created"] >= 1
    assert store.search(scope, "用户 长期 画像", limit=10)


def test_consolidation_respects_min_atom_confidence(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    store.add_atom(MemoryAtom(memory_type="project_context", content="低置信事实", scope=scope, keywords=["置信"], confidence=0.4))
    store.add_atom(MemoryAtom(memory_type="project_context", content="高置信事实一", scope=scope, keywords=["置信"], confidence=0.9))
    store.add_atom(MemoryAtom(memory_type="project_context", content="高置信事实二", scope=scope, keywords=["置信"], confidence=0.9))

    result = consolidate_due_memories(store, scope=scope, min_atom_confidence=0.8)

    assert result["atoms_scanned"] == 2
    assert result["blocks_created"] == 1


def test_consolidation_respects_block_limit(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    for keyword in ["一", "二", "三"]:
        store.add_atom(MemoryAtom(memory_type="preference", content=f"用户偏好主题{keyword}", scope=scope, keywords=[keyword], tags=[keyword], stability=MemoryStability.STABLE))

    consolidate_due_memories(store, scope=scope)
    result = consolidate_due_memories(store, scope=scope, block_limit=1)

    assert result["profiles_created"] + result["profiles_updated"] >= 1

def test_consolidation_uses_llm_summary_for_large_atom_group(tmp_path, mock_config, monkeypatch):
    config = replace(mock_config, openai_api_key="key", openai_base_url="https://llm.example/v1")
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    for content in ["用户偏好中文回答", "用户要求中文解释代码", "用户希望中文总结"]:
        store.add_atom(MemoryAtom(memory_type="preference", content=content, scope=scope, keywords=["中文"], tags=["回答"], stability=MemoryStability.STABLE))

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return '{"choices":[{"message":{"content":"{\\"summary\\":\\"用户稳定偏好使用中文完成代码解释和总结。\\"}"}}]}'.encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: FakeResponse())

    consolidate_due_memories(store, scope=scope, config=config)
    results = store.search(scope, "中文 总结", limit=10)

    assert any("稳定偏好" in item.content for item in results if item.level == "L2")
