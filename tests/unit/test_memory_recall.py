from openhachimi_agent.memory.formatting import format_memory_context
from openhachimi_agent.memory.models import MemoryContext, MemoryScope, MemorySearchResult
from openhachimi_agent.memory.recall import get_memory_store


def test_format_memory_context_includes_xml_tags():
    context = MemoryContext(
        scope=MemoryScope(),
        query="中文",
        results=[
            MemorySearchResult(
                id="m1",
                level="L1",
                content="用户偏好中文。",
                score=1.0,
                memory_type="preference",
                confidence=0.9,
                updated_at="now",
                source="bm25",
                metadata={"stability": "stable", "access_count": 3, "created_at": "then", "valid_until": "2026-11-01T08:00:00+00:00"},
            )
        ],
    )

    text = format_memory_context(context)

    assert "<memory-context>" in text
    assert "用户偏好中文" in text
    assert 'id="m1"' in text
    assert 'type="preference"' in text
    assert 'valid_until="2026-11-01"' in text
    # 诊断字段不再注入给模型
    for noisy in ("source=", "score=", "confidence=", "access_count=", "stability="):
        assert noisy not in text
    assert "</memory-context>" in text


def test_get_memory_store_reuses_instance_for_same_db_path(mock_config):
    first = get_memory_store(mock_config)
    second = get_memory_store(mock_config)

    assert first is second


# ── 召回链路:去重与意图加权 ─────────────────────────────────────────────

from types import SimpleNamespace

from openhachimi_agent.memory import recall as recall_module
from openhachimi_agent.memory.models import MemoryAtom
from openhachimi_agent.memory.recall import apply_intent_boost, dedupe_by_content, recall_memories


def _result(result_id, content, memory_type="user_preference", *, level="L1", score=1.0, predicate="prefers"):
    return MemorySearchResult(
        id=result_id,
        level=level,
        content=content,
        score=score,
        memory_type=memory_type,
        confidence=0.9,
        updated_at="now",
        metadata={"subject": "user", "predicate": predicate},
    )


def test_dedupe_keeps_distinct_preferences_with_same_subject_and_predicate():
    results = [
        _result("a", "使用中文回答"),
        _result("b", "回答保持简洁"),
        _result("c", "喜欢深色主题"),
    ]

    kept = dedupe_by_content(results)

    assert {item.id for item in kept} == {"a", "b", "c"}


def test_dedupe_drops_same_content_and_keeps_highest_score():
    results = [
        _result("low", "使用中文回答", score=0.5),
        _result("high", "使用中文回答。", score=0.9),
        _result("spaced", " 使用 中文 回答 ", score=0.7),
    ]

    kept = dedupe_by_content(results)

    assert [item.id for item in kept] == ["high"]


def test_dedupe_does_not_merge_opposite_meaning():
    kept = dedupe_by_content([_result("a", "用中文回答"), _result("b", "不要用中文回答")])

    assert {item.id for item in kept} == {"a", "b"}


def test_intent_boost_keeps_every_type_and_reorders():
    results = [
        _result("pref", "使用中文回答", "user_preference", score=1.0),
        _result("fact", "项目用 PydanticAI", "project_fact", score=0.9),
        _result("legacy", "用户是后端工程师", "fact", score=0.8),
        _result("profile", "用户长期画像", "user_profile", level="L3", score=0.7),
    ]

    boosted = apply_intent_boost(results, "coding")

    assert {item.id for item in boosted} == {"pref", "fact", "legacy", "profile"}
    # coding 意图下 project_fact 加权后排到最前
    assert boosted[0].id == "fact"


def test_recall_keeps_user_preferences_for_coding_query(mock_config, monkeypatch):
    """修 bug 时仍应召回用户偏好,且主谓相同的多条偏好不被合并。"""
    monkeypatch.setattr(
        recall_module,
        "EmbeddingProvider",
        lambda _cfg: SimpleNamespace(embed_sync=lambda _text: SimpleNamespace(degraded=True, reason="test", vector=[])),
    )
    scope = MemoryScope(role_name="default", session_id="s1")
    store = get_memory_store(mock_config)
    for content in ("使用中文回答", "回答保持简洁", "代码注释写中文"):
        store.add_atom(MemoryAtom(memory_type="user_preference", content=content, scope=scope, predicate="prefers"))

    context = recall_memories(mock_config, scope, "帮我修复这个 bug")

    contents = {item.content for item in context.results}
    assert {"使用中文回答", "回答保持简洁", "代码注释写中文"} <= contents


def test_select_by_level_budget_excludes_task_reference_blocks():
    from openhachimi_agent.memory.recall import select_by_level_budget

    results = [
        _result("atom", "整理过 example.com", "task_reference"),
        _result("block", "task_reference: example.com", "task_reference", level="L2"),
        _result("pref", "使用中文回答"),
    ]

    selected = select_by_level_budget(results, final_l1_top_k=10, final_l2_top_k=4, include_l3_profile=True)

    assert [item.id for item in selected] == ["pref"]


def _no_vectors(monkeypatch):
    monkeypatch.setattr(
        recall_module,
        "EmbeddingProvider",
        lambda _cfg: SimpleNamespace(embed_sync=lambda _text: SimpleNamespace(degraded=True, reason="test", vector=[])),
    )


def _access_counts(store):
    with store.connect() as conn:
        return {row["content"]: row["access_count"] for row in conn.execute("SELECT content, access_count FROM memory_atoms")}


def test_core_memories_injected_even_when_query_unrelated(mock_config, monkeypatch):
    """偏好 / 约束每轮都带上,不依赖当前消息能否检索命中。"""
    _no_vectors(monkeypatch)
    scope = MemoryScope(role_name="default", session_id="s1")
    store = get_memory_store(mock_config)
    store.add_atom(MemoryAtom(memory_type="user_preference", content="使用中文回答", scope=scope))
    store.add_atom(MemoryAtom(memory_type="user_constraint", content="不要使用表情符号", scope=scope))

    context = recall_memories(mock_config, scope, "今天天气怎么样")

    assert {item.content for item in context.results} == {"使用中文回答", "不要使用表情符号"}
    assert all(item.source == "core" for item in context.results)


def test_unmatched_non_core_memories_not_injected(mock_config, monkeypatch):
    """检索没命中时不再把"最近几条"项目事实塞进来,需要时由模型调 search_memory 查。"""
    _no_vectors(monkeypatch)
    scope = MemoryScope(role_name="default", session_id="s1")
    store = get_memory_store(mock_config)
    store.add_atom(MemoryAtom(memory_type="project_fact", content="项目使用 PydanticAI 框架", scope=scope))

    unrelated = recall_memories(mock_config, scope, "今天天气怎么样")
    related = recall_memories(mock_config, scope, "这个项目用的什么框架")

    assert unrelated.results == []
    assert [item.content for item in related.results] == ["项目使用 PydanticAI 框架"]


def test_recall_touches_only_retrieved_memories(mock_config, monkeypatch):
    """只有检索命中才算一次访问;常驻记忆每轮都注入,不刷新访问次数。"""
    _no_vectors(monkeypatch)
    scope = MemoryScope(role_name="default", session_id="s1")
    store = get_memory_store(mock_config)
    store.add_atom(MemoryAtom(memory_type="user_preference", content="使用中文回答", scope=scope))
    store.add_atom(MemoryAtom(memory_type="project_fact", content="项目使用 PydanticAI 框架", scope=scope))

    recall_memories(mock_config, scope, "这个项目用的什么框架")

    assert _access_counts(store) == {"使用中文回答": 0, "项目使用 PydanticAI 框架": 1}


def test_retrieved_duplicate_of_core_memory_is_dropped(mock_config, monkeypatch):
    _no_vectors(monkeypatch)
    scope = MemoryScope(role_name="default", session_id="s1")
    store = get_memory_store(mock_config)
    store.add_atom(MemoryAtom(memory_type="user_preference", content="回答使用中文", scope=scope))
    store.add_atom(MemoryAtom(memory_type="project_fact", content="回答使用中文。", scope=scope))

    context = recall_memories(mock_config, scope, "回答使用中文")

    assert [item.content for item in context.results] == ["回答使用中文"]
