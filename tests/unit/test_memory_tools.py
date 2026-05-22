import importlib.util
from pathlib import Path
from types import SimpleNamespace

from openhachimi_agent.core.deps import AgentDeps
from openhachimi_agent.memory.models import MemoryScope
from openhachimi_agent.memory.recall import get_memory_store

_SPEC = importlib.util.spec_from_file_location(
    "memory_tools_module",
    Path(__file__).resolve().parents[2] / "openhachimi_agent" / "tools" / "memory.py",
)
memory_tools = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(memory_tools)
_normalize_tags = memory_tools._normalize_tags


def test_normalize_tags_accepts_json_string():
    assert _normalize_tags('["location", "timezone", "user-preference"]') == [
        "location",
        "timezone",
        "user-preference",
    ]


def test_normalize_tags_accepts_comma_string():
    assert _normalize_tags("language, user-preference") == ["language", "user-preference"]


def test_update_memory_preserves_atom_id_trace_and_access_count(mock_config, mock_browser_manager):
    deps = AgentDeps(
        config=mock_config,
        session_id="s1",
        browser_manager=mock_browser_manager,
        memory_scope=MemoryScope(role_name="default", session_id="s1"),
    )
    ctx = SimpleNamespace(deps=deps)
    store = get_memory_store(mock_config)
    stored = memory_tools.remember(ctx, "旧内容", memory_type="fact")
    memory_id = stored["id"]
    with store.connect() as conn:
        conn.execute(
            "UPDATE memory_atoms SET evidence_turn_ids_json = ?, source_quote = ? WHERE id = ?",
            ('["turn-1"]', "原始引用", memory_id),
        )
    store.touch([memory_id])

    result = memory_tools.update_memory(ctx, memory_id, content="新内容")

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, content, evidence_turn_ids_json, source_quote, access_count, status FROM memory_atoms WHERE id = ?",
            (memory_id,),
        ).fetchall()
    assert result["updated"] is True
    assert result["id"] == memory_id
    assert len(rows) == 1
    assert rows[0]["content"] == "新内容"
    assert rows[0]["evidence_turn_ids_json"] == '["turn-1"]'
    assert rows[0]["source_quote"] == "原始引用"
    assert rows[0]["access_count"] == 1
    assert rows[0]["status"] == "active"


def test_remember_queues_embedding_job(mock_config, mock_browser_manager):
    deps = AgentDeps(
        config=mock_config,
        session_id="s1",
        browser_manager=mock_browser_manager,
        memory_scope=MemoryScope(role_name="default", session_id="s1"),
    )
    ctx = SimpleNamespace(deps=deps)

    result = memory_tools.remember(ctx, "请记住我喜欢语义搜索召回", memory_type="preference")

    store = get_memory_store(mock_config)
    with store.connect() as conn:
        job = conn.execute("SELECT job_type, payload_json FROM memory_jobs WHERE job_type = 'embed_memory_item'").fetchone()
    assert result["stored"] is True
    assert result["embedding_status"] == "queued"
    assert job is not None
