from openhachimi_agent.memory.models import MemoryAtom, MemoryScope
from openhachimi_agent.memory.store import MemoryStore
from openhachimi_agent.memory.conflicts import resolve_atom_conflict


def test_duplicate_atom_is_deduped(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default")
    old = MemoryAtom(memory_type="preference", content="用户喜欢简洁回答", scope=scope, subject="user", predicate="likes", object="简洁回答")
    new = MemoryAtom(memory_type="preference", content="用户喜欢简洁回答", scope=scope, subject="user", predicate="likes", object="简洁回答")
    store.add_atom(old)

    decision = resolve_atom_conflict(store, new)

    assert decision.action == "dedupe"
    assert decision.winner_id == old.id


def test_vector_similarity_dedupes_same_slot_different_content(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default")
    old = MemoryAtom(memory_type="preference", content="用户喜欢中文回答", scope=scope, subject="user", predicate="likes", object="中文回答")
    new = MemoryAtom(memory_type="preference", content="用户偏好汉语答复", scope=scope, subject="user", predicate="likes", object="汉语答复")
    store.add_atom(old)
    store.save_vector(old.id, "L1", "test", [1.0, 0.0, 0.0])

    decision = resolve_atom_conflict(store, new, embedding_vector=[0.99, 0.01, 0.0], embedding_model="test")

    # 阈值内统一去重(保留旧者,丢弃新者),不再 supersede
    assert decision.action == "dedupe"
    assert decision.winner_id == old.id
    assert decision.reason.startswith("vector_similar")


def test_vector_similarity_does_not_supersede_different_slot(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default")
    old = MemoryAtom(memory_type="preference", content="用户喜欢中文回答", scope=scope, subject="user", predicate="likes", object="中文回答")
    new = MemoryAtom(memory_type="preference", content="用户使用中文回答", scope=scope, subject="user", predicate="uses", object="中文回答")
    store.add_atom(old)
    store.save_vector(old.id, "L1", "test", [1.0, 0.0, 0.0])

    decision = resolve_atom_conflict(store, new, embedding_vector=[1.0, 0.0, 0.0], embedding_model="test")

    assert decision.action == "insert"


def test_same_content_still_dedupes_before_vector(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default")
    old = MemoryAtom(memory_type="preference", content="我喜欢中文", scope=scope, subject="user", predicate="likes", object="中文")
    new = MemoryAtom(memory_type="preference", content="我喜欢中文", scope=scope, subject="user", predicate="likes", object="中文")
    store.add_atom(old)
    store.save_vector(old.id, "L1", "test", [1.0, 0.0, 0.0])

    decision = resolve_atom_conflict(store, new, embedding_vector=[1.0, 0.0, 0.0], embedding_model="test")

    assert decision.action == "dedupe"
    assert decision.winner_id == old.id
