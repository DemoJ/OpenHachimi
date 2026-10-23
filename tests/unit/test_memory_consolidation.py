from dataclasses import replace

from openhachimi_agent.memory.consolidation import _llm_summarize, consolidate_due_memories
from openhachimi_agent.memory.llm import MemorySummaryOutput
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
    captured = {}

    def fake_run_memory_summary(config_arg, *, system_prompt, payload):
        captured["config"] = config_arg
        captured["system_prompt"] = system_prompt
        captured["payload"] = payload
        return MemorySummaryOutput(summary="用户稳定偏好使用中文完成代码解释和总结。")

    monkeypatch.setattr("openhachimi_agent.memory.consolidation.run_memory_summary", fake_run_memory_summary)

    consolidate_due_memories(store, scope=scope, config=config)
    results = store.search(scope, "中文 总结", limit=10)

    assert any("稳定偏好" in item.content for item in results if item.level == "L2")
    assert captured["config"] is config
    assert "长期记忆摘要" in captured["system_prompt"]
    assert captured["payload"]["kind"] == "block"
    assert set(captured["payload"]["evidence"]["atoms"]) == {"用户偏好中文回答", "用户要求中文解释代码", "用户希望中文总结"}


def test_llm_summarize_truncates_summary(mock_config, monkeypatch):
    config = replace(mock_config, openai_api_key="key", openai_base_url="https://llm.example/v1")

    def fake_run_memory_summary(*args, **kwargs):
        return MemorySummaryOutput(summary="中" * 900)

    monkeypatch.setattr("openhachimi_agent.memory.consolidation.run_memory_summary", fake_run_memory_summary)

    result = _llm_summarize("block", {"atoms": ["用户偏好中文回答"]}, config)

    assert result == "中" * 800


def test_llm_summary_degrades_to_empty_string(mock_config, monkeypatch):
    config = replace(mock_config, openai_api_key="key", openai_base_url="https://llm.example/v1")

    def fake_run_memory_summary(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("openhachimi_agent.memory.consolidation.run_memory_summary", fake_run_memory_summary)

    assert _llm_summarize("block", {"atoms": ["用户偏好中文回答"]}, config) == ""


def _block_rows(store):
    with store.connect() as conn:
        return conn.execute("SELECT id, status, summary, details, atom_ids_json FROM memory_blocks").fetchall()


def _two_atom_block(store, scope):
    first = MemoryAtom(memory_type="user_preference", content="用户偏好中文回答", scope=scope, keywords=["中文"], tags=["回答"], stability=MemoryStability.STABLE)
    second = MemoryAtom(memory_type="user_preference", content="用户要求中文解释代码", scope=scope, keywords=["中文"], tags=["回答"], stability=MemoryStability.STABLE)
    store.add_atom(first)
    store.add_atom(second)
    consolidate_due_memories(store, scope=scope)
    return first, second


def test_block_rebuilt_without_forgotten_atom(tmp_path):
    """主题块里的某条记忆被删掉后,块的摘要和关联只保留剩下的记忆。"""
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    first, second = _two_atom_block(store, scope)

    store.forget(scope, first.id)
    result = consolidate_due_memories(store, scope=scope)

    assert result["blocks_rebuilt"] == 1
    (block,) = [row for row in _block_rows(store) if row["status"] == "active"]
    assert "用户偏好中文回答" not in block["summary"] + block["details"]
    assert "用户要求中文解释代码" in block["summary"]
    assert first.id not in block["atom_ids_json"]


def test_block_and_profile_archived_when_all_atoms_gone(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    first, second = _two_atom_block(store, scope)
    assert store.get_active_profile("local", "local", "default", "user_profile") is not None

    store.forget(scope, f"{first.id},{second.id}", hard_delete=True)
    result = consolidate_due_memories(store, scope=scope)

    assert result["blocks_archived"] == 1
    assert result["profiles_archived"] == 1
    assert all(row["status"] == "archived" for row in _block_rows(store))
    assert store.get_active_profile("local", "local", "default", "user_profile") is None


def test_profile_rebuilt_from_current_blocks_only(tmp_path):
    """画像每次按当前 block 重建,已经失效的旧条目不会残留。"""
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    store.add_atom(MemoryAtom(memory_type="user_preference", content="用户偏好深色主题", scope=scope, keywords=["主题"], tags=["主题"], stability=MemoryStability.STABLE))
    old_constraint = MemoryAtom(memory_type="user_constraint", content="用户要求使用 Python2", scope=scope, keywords=["Python"], tags=["语言"], stability=MemoryStability.STABLE)
    store.add_atom(old_constraint)
    consolidate_due_memories(store, scope=scope)
    assert "Python2" in store.get_active_profile("local", "local", "default", "user_profile").summary

    store.forget(scope, old_constraint.id)
    consolidate_due_memories(store, scope=scope)

    profile = store.get_active_profile("local", "local", "default", "user_profile")
    assert "Python2" not in profile.summary
    assert profile.constraints == {}
