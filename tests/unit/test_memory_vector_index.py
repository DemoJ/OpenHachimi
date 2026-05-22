from openhachimi_agent.memory.models import MemoryAtom, MemoryScope
from openhachimi_agent.memory.store import MemoryStore
from openhachimi_agent.memory.vector_index import SQLiteVecIndex, cosine_similarity, shard_keys_for_vector


def test_vector_shard_helpers():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert shard_keys_for_vector([1.0, -0.5, 0.1])


def test_vector_shard_search_returns_nearest(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    first = MemoryAtom(memory_type="fact", content="alpha", scope=scope)
    second = MemoryAtom(memory_type="fact", content="beta", scope=scope)
    store.add_atom(first)
    store.add_atom(second)
    store.save_vector(first.id, "L1", "test", [1.0, 0.0, 0.0])
    store.save_vector(second.id, "L1", "test", [0.0, 1.0, 0.0])

    results = store.vector_search(scope, [1.0, 0.0, 0.0], model="test")

    assert results[0].id == first.id


def test_sqlite_vec_index_reports_availability_flag():
    index = SQLiteVecIndex()

    assert isinstance(index.available, bool)
