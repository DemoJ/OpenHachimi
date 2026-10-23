# pyrefly: ignore [missing-import]
"""新记忆写入时由 LLM 对照相近旧记忆决定 ADD / UPDATE / DELETE / NONE(参考 mem0)。"""

import pytest

from openhachimi_agent.memory.llm import MemoryUpdateDecision, MemoryUpdateOutput
from openhachimi_agent.memory.models import ExtractedMemory, MemoryAtom, MemoryScope
from openhachimi_agent.memory.store import MemoryStore
from openhachimi_agent.memory.update_decision import apply_extracted_memories

_TARGET = "openhachimi_agent.memory.update_decision.run_memory_update"


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.sqlite3")


@pytest.fixture
def scope():
    return MemoryScope(role_name="default", session_id="s1")


def _statuses(store):
    with store.connect() as conn:
        return {row["content"]: row["status"] for row in conn.execute("SELECT content, status FROM memory_atoms")}


def _fake_llm(monkeypatch, decisions):
    calls = []

    def fake(config, *, system_prompt, payload):
        calls.append(payload)
        return None if decisions is None else MemoryUpdateOutput(decisions=decisions)

    monkeypatch.setattr(_TARGET, fake)
    return calls


def _new(content, memory_type="user_preference"):
    return ExtractedMemory(memory_type=memory_type, content=content)


def test_adds_directly_without_llm_when_no_similar_memory(store, scope, mock_config, monkeypatch):
    calls = _fake_llm(monkeypatch, [])

    result = apply_extracted_memories(store, mock_config, scope, "t1", [_new("使用中文回答")])

    assert result.created == 1
    assert calls == []
    assert _statuses(store) == {"使用中文回答": "active"}


def test_update_archives_old_and_stores_merged_content(store, scope, mock_config, monkeypatch):
    old = MemoryAtom(memory_type="user_preference", content="用户使用 Python2", scope=scope)
    store.add_atom(old)
    calls = _fake_llm(monkeypatch, [MemoryUpdateDecision(index=0, event="UPDATE", target_id=old.id, content="用户使用 Python3")])

    result = apply_extracted_memories(store, mock_config, scope, "t1", [_new("用户改用 Python3")])

    assert (result.created, result.archived) == (1, 1)
    assert calls[0]["existing"][0]["id"] == old.id
    assert _statuses(store) == {"用户使用 Python2": "archived", "用户使用 Python3": "active"}


def test_delete_archives_old_without_storing_new(store, scope, mock_config, monkeypatch):
    old = MemoryAtom(memory_type="user_constraint", content="用户要求回答带表情符号", scope=scope)
    store.add_atom(old)
    _fake_llm(monkeypatch, [MemoryUpdateDecision(index=0, event="delete", target_id=old.id)])

    result = apply_extracted_memories(store, mock_config, scope, "t1", [_new("用户不再要求回答带表情符号", "user_constraint")])

    assert (result.created, result.archived) == (0, 1)
    assert _statuses(store) == {"用户要求回答带表情符号": "archived"}


def test_none_keeps_everything_unchanged(store, scope, mock_config, monkeypatch):
    store.add_atom(MemoryAtom(memory_type="user_preference", content="用户偏好中文回答", scope=scope))
    _fake_llm(monkeypatch, [MemoryUpdateDecision(index=0, event="NONE")])

    result = apply_extracted_memories(store, mock_config, scope, "t1", [_new("用户喜欢用中文回答")])

    assert not result.changed
    assert _statuses(store) == {"用户偏好中文回答": "active"}


def test_unknown_target_id_never_deletes(store, scope, mock_config, monkeypatch):
    """LLM 编造的 id:UPDATE 当新增处理,DELETE 不执行,旧记忆不受影响。"""
    old = MemoryAtom(memory_type="user_preference", content="用户偏好中文回答", scope=scope)
    store.add_atom(old)
    _fake_llm(monkeypatch, [
        MemoryUpdateDecision(index=0, event="UPDATE", target_id="not-a-real-id", content="x"),
        MemoryUpdateDecision(index=1, event="DELETE", target_id="not-a-real-id"),
    ])

    result = apply_extracted_memories(store, mock_config, scope, "t1", [_new("用户偏好中文和英文回答"), _new("用户偏好中文的注释")])

    assert (result.created, result.archived) == (1, 0)
    assert _statuses(store)["用户偏好中文回答"] == "active"


def test_falls_back_to_rules_when_llm_unavailable(store, scope, mock_config, monkeypatch):
    store.add_atom(MemoryAtom(memory_type="user_preference", content="用户偏好中文回答", scope=scope))
    _fake_llm(monkeypatch, None)

    result = apply_extracted_memories(store, mock_config, scope, "t1", [_new("用户偏好中文解释代码")])

    assert result.created == 1
    assert _statuses(store) == {"用户偏好中文回答": "active", "用户偏好中文解释代码": "active"}


def test_exact_duplicate_skipped_without_llm(store, scope, mock_config, monkeypatch):
    store.add_atom(MemoryAtom(memory_type="user_preference", content="使用中文回答", scope=scope))
    calls = _fake_llm(monkeypatch, [])

    result = apply_extracted_memories(store, mock_config, scope, "t1", [_new("使用中文回答")])

    assert not result.changed
    assert calls == []


class _FixedVectors:
    """假 embedding:所有文本都给同一个向量,让新旧记忆在向量上"高度相似"。"""

    def embed_sync(self, _text):
        from types import SimpleNamespace

        return SimpleNamespace(degraded=False, reason="", vector=[1.0, 0.0, 0.0])


def test_llm_add_still_supersedes_highly_similar_same_slot_memory(store, scope, mock_config, monkeypatch):
    """真实模型会把"改用英文"判成 ADD;同一槽位高度相似时仍以新为准,不留两条矛盾偏好。"""
    old = MemoryAtom(memory_type="user_preference", content="用户要求回答使用中文", scope=scope)
    store.add_atom(old)
    store.save_vector(old.id, "L1", mock_config.memory.embedding.model, [1.0, 0.0, 0.0])
    _fake_llm(monkeypatch, [MemoryUpdateDecision(index=0, event="ADD")])

    result = apply_extracted_memories(
        store, mock_config, scope, "t1", [_new("用户要求以后改用英文回答")], embedding_provider=_FixedVectors()
    )

    assert (result.created, result.archived) == (1, 1)
    assert _statuses(store) == {"用户要求回答使用中文": "archived", "用户要求以后改用英文回答": "active"}


def test_target_id_with_extra_text_is_salvaged(store, scope, mock_config, monkeypatch):
    """较弱的模型会往 target_id 里混解释文字;其中恰好包含一个候选 id 时照常使用。"""
    old = MemoryAtom(memory_type="user_preference", content="用户使用 Python2", scope=scope)
    store.add_atom(old)
    _fake_llm(monkeypatch, [
        MemoryUpdateDecision(index=0, event="UPDATE", target_id=f"{old.id} worth noting? use given id", content="用户使用 Python3"),
    ])

    result = apply_extracted_memories(store, mock_config, scope, "t1", [_new("用户改用 Python3")])

    assert (result.created, result.archived) == (1, 1)
    assert _statuses(store) == {"用户使用 Python2": "archived", "用户使用 Python3": "active"}
