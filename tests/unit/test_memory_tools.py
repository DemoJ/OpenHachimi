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


def test_forget_memory_rejects_wildcard(mock_config, mock_browser_manager):
    """forget_memory(*) 应被显式拒绝, 防止误删全库 + 防止 FTS5 'unknown special query'。"""
    deps = AgentDeps(
        config=mock_config,
        session_id="s1",
        browser_manager=mock_browser_manager,
        memory_scope=MemoryScope(role_name="default", session_id="s1"),
    )
    ctx = SimpleNamespace(deps=deps)

    for wildcard in ("*", "%", "**", "all", "ALL", "  *  "):
        result = memory_tools.forget_memory(ctx, wildcard, mode="hard_delete")
        assert result["deleted"] == 0
        assert "error" in result, f"expected wildcard {wildcard!r} to be rejected"


def test_forget_memory_handles_empty_query(mock_config, mock_browser_manager):
    """空 query 应安全失败而非抛异常。"""
    deps = AgentDeps(
        config=mock_config,
        session_id="s1",
        browser_manager=mock_browser_manager,
        memory_scope=MemoryScope(role_name="default", session_id="s1"),
    )
    ctx = SimpleNamespace(deps=deps)

    result = memory_tools.forget_memory(ctx, "   ", mode="hard_delete")
    assert result["deleted"] == 0
    assert "error" in result


def test_forget_memory_with_short_non_id_query_does_not_crash(mock_config, mock_browser_manager):
    """模型传入短查询词(<16 字符且非 hex)走 FTS 兜底, 不应抛 'unknown special query'。"""
    deps = AgentDeps(
        config=mock_config,
        session_id="s1",
        browser_manager=mock_browser_manager,
        memory_scope=MemoryScope(role_name="default", session_id="s1"),
    )
    ctx = SimpleNamespace(deps=deps)

    # 各种容易触发 FTS5 特殊字符报错的查询
    for tricky in ("()", "---", "?", "\"\"", "OR AND"):
        result = memory_tools.forget_memory(ctx, tricky, mode="soft_delete")
        # 不应抛异常;结果 deleted=0 也可接受(没有匹配)
        assert "deleted" in result


def _memory_ctx(mock_config, mock_browser_manager):
    deps = AgentDeps(
        config=mock_config,
        session_id="s1",
        browser_manager=mock_browser_manager,
        memory_scope=MemoryScope(role_name="default", session_id="s1"),
    )
    return SimpleNamespace(deps=deps)


def _stored_type(mock_config, memory_id):
    with get_memory_store(mock_config).connect() as conn:
        return conn.execute("SELECT memory_type FROM memory_atoms WHERE id = ?", (memory_id,)).fetchone()[0]


def test_remember_defaults_to_user_preference(mock_config, mock_browser_manager):
    ctx = _memory_ctx(mock_config, mock_browser_manager)

    result = memory_tools.remember(ctx, "以后回答都用中文")

    assert result["memory_type"] == "user_preference"
    assert _stored_type(mock_config, result["id"]) == "user_preference"


def test_remember_normalizes_legacy_and_unknown_types(mock_config, mock_browser_manager):
    ctx = _memory_ctx(mock_config, mock_browser_manager)

    legacy = memory_tools.remember(ctx, "用户是后端工程师", memory_type="fact")
    project = memory_tools.remember(ctx, "项目基于 PydanticAI", memory_type="project_fact")
    unknown = memory_tools.remember(ctx, "喜欢深色主题", memory_type="whatever")

    assert _stored_type(mock_config, legacy["id"]) == "user_trait"
    assert _stored_type(mock_config, project["id"]) == "project_fact"
    assert _stored_type(mock_config, unknown["id"]) == "user_preference"


def test_remember_dedupes_identical_content(mock_config, mock_browser_manager):
    ctx = _memory_ctx(mock_config, mock_browser_manager)

    first = memory_tools.remember(ctx, "以后回答都用中文")
    second = memory_tools.remember(ctx, "以后回答都用中文")

    assert second["id"] == first["id"]
    assert second.get("deduplicated") is True
    with get_memory_store(mock_config).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0] == 1


def test_forget_memory_queues_consolidation(mock_config, mock_browser_manager):
    """删掉的记忆可能已被合并进主题块和画像,删除后要排队重新合并。"""
    ctx = _memory_ctx(mock_config, mock_browser_manager)
    stored = memory_tools.remember(ctx, "以后回答都用中文")

    memory_tools.forget_memory(ctx, stored["id"])

    with get_memory_store(mock_config).connect() as conn:
        jobs = conn.execute("SELECT COUNT(*) FROM memory_jobs WHERE job_type = 'consolidate_scope'").fetchone()[0]
    assert jobs == 1


def test_remember_marks_turn_for_capture(mock_config, mock_browser_manager):
    from openhachimi_agent.memory.capture import REMEMBERED_THIS_TURN_KEY

    ctx = _memory_ctx(mock_config, mock_browser_manager)

    memory_tools.remember(ctx, "以后回答都用中文")

    assert ctx.deps.session_state[REMEMBERED_THIS_TURN_KEY] is True
