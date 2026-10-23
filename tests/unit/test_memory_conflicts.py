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


def test_vector_similarity_supersedes_same_slot_with_newer_statement(tmp_path):
    """同一槽位、向量相似但内容不同:以新说法为准,旧记忆归档。

    向量分不清"换个说法"和"改了主意",以新为准对两种情况都对;保留旧者在
    用户改主意(如"用 Python2"→"用 Python3")时是错的。
    """
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default")
    old = MemoryAtom(memory_type="preference", content="用户使用 Python2", scope=scope, subject="user", predicate="uses", object="Python2")
    new = MemoryAtom(memory_type="preference", content="用户改用 Python3", scope=scope, subject="user", predicate="uses", object="Python3")
    store.add_atom(old)
    store.save_vector(old.id, "L1", "test", [1.0, 0.0, 0.0])

    decision = resolve_atom_conflict(store, new, embedding_vector=[0.99, 0.01, 0.0], embedding_model="test")

    assert decision.action == "supersede"
    assert decision.winner_id == new.id
    assert decision.loser_id == old.id
    with store.connect() as conn:
        status = conn.execute("SELECT status FROM memory_atoms WHERE id = ?", (old.id,)).fetchone()[0]
    assert status == "archived"


def test_vector_similarity_keeps_old_when_new_is_low_confidence(tmp_path):
    """新记忆置信度本身很低(抽取器没把握)时不覆盖旧记忆。"""
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default")
    old = MemoryAtom(memory_type="preference", content="用户喜欢中文回答", scope=scope, subject="user", predicate="likes", object="中文回答", confidence=0.95)
    new = MemoryAtom(memory_type="preference", content="用户偏好汉语答复", scope=scope, subject="user", predicate="likes", object="汉语答复", confidence=0.5)
    store.add_atom(old)
    store.save_vector(old.id, "L1", "test", [1.0, 0.0, 0.0])

    decision = resolve_atom_conflict(store, new, embedding_vector=[0.99, 0.01, 0.0], embedding_model="test")

    assert decision.action == "dedupe"
    assert decision.winner_id == old.id


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


def test_vector_similarity_supersedes_even_when_new_confidence_lower(tmp_path):
    """用户改主意时新记忆置信度常比旧的低(真实对话里 0.7 对 0.9),仍应以新为准。"""
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default")
    old = MemoryAtom(memory_type="preference", content="用户要求回答使用中文", scope=scope, subject="user", predicate="states", confidence=0.9)
    new = MemoryAtom(memory_type="preference", content="用户要求以后改用英文回答", scope=scope, subject="user", predicate="states", confidence=0.7)
    store.add_atom(old)
    store.save_vector(old.id, "L1", "test", [1.0, 0.0, 0.0])

    decision = resolve_atom_conflict(store, new, embedding_vector=[0.99, 0.01, 0.0], embedding_model="test")

    assert decision.action == "supersede"
    assert decision.winner_id == new.id
