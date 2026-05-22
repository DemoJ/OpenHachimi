from openhachimi_agent.memory.models import MemoryAtom, MemoryScope
from openhachimi_agent.memory.store import MemoryStore


def test_memory_store_lists_atoms_without_keyword_search(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    atom = MemoryAtom(memory_type="fact", content="alpha beta gamma", scope=scope)
    store.add_atom(atom)

    results = store.list_memories(scope, limit=10)

    assert [item.id for item in results] == [atom.id]
    assert results[0].source == "list"


def test_memory_store_list_filters_by_memory_type(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    fact = MemoryAtom(memory_type="fact", content="alpha", scope=scope)
    preference = MemoryAtom(memory_type="preference", content="beta", scope=scope)
    store.add_atom(fact)
    store.add_atom(preference)

    results = store.list_memories(scope, memory_type="fact", limit=10)

    assert [item.id for item in results] == [fact.id]
