from openhachimi_agent.memory.models import MemoryAtom, MemoryScope
from openhachimi_agent.memory.store import MemoryStore


def test_memory_store_initializes_and_searches_atoms(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    atom = MemoryAtom(
        memory_type="preference",
        content="用户偏好使用中文回答，并且要求务实具体。",
        scope=scope,
        keywords=["中文", "务实", "具体"],
        confidence=0.9,
    )

    store.add_atom(atom)
    results = store.search(scope, "中文 回答", limit=5)

    assert results
    assert results[0].id == atom.id
    assert results[0].level == "L1"
    assert "中文" in results[0].content
    assert store.stats()["atoms"] == 1


def test_memory_store_saves_vectors_and_reports_embedding_stats(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    atom = MemoryAtom(memory_type="fact", content="alpha beta", scope=scope, keywords=["alpha"])
    store.add_atom(atom)

    store.save_vector(atom.id, "L1", "test-embedding", [1.0, 0.0, 0.0])
    results = store.vector_search(scope, [1.0, 0.0, 0.0], model="test-embedding")
    stats = store.stats()

    assert results[0].id == atom.id
    assert results[0].source == "vector"
    assert stats["embeddings_ready"] == 1
    assert stats["vectors"] == 1


def test_memory_store_forget_soft_deletes(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    atom = MemoryAtom(memory_type="fact", content="OpenHachimi 使用 PydanticAI。", scope=scope, keywords=["OpenHachimi", "PydanticAI"])
    store.add_atom(atom)

    deleted = store.forget(scope, atom.id)

    assert deleted == 1
    assert store.search(scope, "PydanticAI", limit=5) == []
