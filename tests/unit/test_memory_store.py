from datetime import datetime, timedelta, timezone

from openhachimi_agent.memory.models import MemoryAtom, MemoryBlock, MemoryJob, MemoryProfile, MemoryScope, MemoryStability
from openhachimi_agent.memory.store import MemoryStore


def test_memory_store_reuses_thread_local_connection(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")

    with store.connect() as first:
        first_id = id(first)
    with store.connect() as second:
        second_id = id(second)

    assert first_id == second_id
    store.close()


def test_memory_store_claims_and_completes_due_jobs(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    job_id = store.enqueue_job(MemoryJob(job_type="maintenance", payload={}))

    jobs = store.claim_due_jobs()

    assert [job.id for job in jobs] == [job_id]
    assert jobs[0].attempts == 1
    assert jobs[0].status.value == "running"
    assert store.claim_due_jobs() == []
    store.complete_job(job_id)
    assert store.claim_due_jobs() == []


def test_memory_store_retries_failed_job_until_max_attempts(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    job_id = store.enqueue_job(MemoryJob(job_type="maintenance", payload={}, max_attempts=1))
    job = store.claim_due_jobs()[0]

    store.fail_job(job.id, "boom")

    with store.connect() as conn:
        row = conn.execute("SELECT status, last_error FROM memory_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["last_error"] == "boom"


def test_memory_store_reclaims_stale_running_job(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    job_id = store.enqueue_job(MemoryJob(job_type="maintenance", payload={}, locked_at=stale, status="running"))

    jobs = store.claim_due_jobs(lock_seconds=60)

    assert [job.id for job in jobs] == [job_id]
    assert jobs[0].attempts == 1


def test_enqueue_unique_job_uses_literal_dedupe_key(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")

    first = store.enqueue_unique_job("maintenance", {"value": 1}, dedupe_key="abc_def%ghi\"x")
    same = store.enqueue_unique_job("maintenance", {"value": 2}, dedupe_key="abc_def%ghi\"x")
    different = store.enqueue_unique_job("maintenance", {"value": 3}, dedupe_key="abcXdefYghi\"x")

    assert same == first
    assert different != first


def test_add_atom_upsert_preserves_created_at_access_count_and_trace_fields(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    atom = MemoryAtom(
        memory_type="fact",
        content="原始记忆",
        scope=scope,
        evidence_turn_ids=["turn-1"],
        source_quote="原始引用",
        created_at="2026-01-01T00:00:00+00:00",
    )
    store.add_atom(atom)
    store.touch([atom.id])
    replacement = MemoryAtom(
        id=atom.id,
        memory_type="fact",
        content="更新后的记忆",
        scope=scope,
        created_at="2026-02-01T00:00:00+00:00",
    )

    store.add_atom(replacement)

    with store.connect() as conn:
        row = conn.execute(
            "SELECT content, evidence_turn_ids_json, source_quote, created_at, access_count FROM memory_atoms WHERE id = ?",
            (atom.id,),
        ).fetchone()
    assert row["content"] == "更新后的记忆"
    assert row["evidence_turn_ids_json"] == '["turn-1"]'
    assert row["source_quote"] == "原始引用"
    assert row["created_at"] == "2026-01-01T00:00:00+00:00"
    assert row["access_count"] == 1


def test_memory_store_expires_due_atoms(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    atom = MemoryAtom(memory_type="fact", content="临时事实会过期", scope=scope, keywords=["临时事实"], valid_until="2000-01-01T00:00:00+00:00")
    store.add_atom(atom)

    expired = store.expire_due_atoms(now="2026-01-01T00:00:00+00:00")

    assert expired == 1
    assert store.search(scope, "临时事实", limit=5) == []


def test_memory_store_initializes_and_searches_atoms(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    atom = MemoryAtom(
        memory_type="preference",
        content="用户偏好使用中文回答，并且要求务实具体。",
        scope=scope,
        keywords=["中文", "务实", "具体"],
        confidence=0.9,
        stability=MemoryStability.STABLE,
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
    assert results[0].source in {"sqlite-vec", "vector-shard", "vector", "sqlite-vec+vector-shard+vector", "vector-shard+vector"}
    assert stats["embeddings_ready"] == 1
    assert stats["vectors"] == 1
    assert stats["vector_shards"] > 0


def test_vector_search_merges_available_backends(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    first = MemoryAtom(memory_type="fact", content="alpha beta", scope=scope, keywords=["alpha"])
    second = MemoryAtom(memory_type="fact", content="gamma delta", scope=scope, keywords=["gamma"])
    store.add_atom(first)
    store.add_atom(second)
    store.save_vector(first.id, "L1", "test-embedding", [1.0, 0.0, 0.0])
    store.save_vector(second.id, "L1", "test-embedding", [0.0, 1.0, 0.0])
    monkeypatch.setattr(store.sqlite_vec_index, "search", lambda *args, **kwargs: [])

    results = store.vector_search(scope, [0.7, 0.7, 0.0], model="test-embedding", limit=10)

    ids = {result.id for result in results}
    assert {first.id, second.id}.issubset(ids)


def test_memory_store_forget_soft_deletes_and_clears_fts(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    atom = MemoryAtom(memory_type="fact", content="OpenHachimi 使用 PydanticAI。", scope=scope, keywords=["OpenHachimi", "PydanticAI"])
    block = MemoryBlock(block_type="fact", title="框架", summary="项目使用 PydanticAI", scope=scope, keywords=["PydanticAI"])
    profile = MemoryProfile(profile_type="user_profile", tenant_id=scope.tenant_id, user_id=scope.user_id, role_name=scope.role_name, title="画像", summary="偏好 PydanticAI")
    store.add_atom(atom)
    store.add_block(block)
    store.add_profile(profile)

    deleted = store.forget(scope, f"{atom.id},{block.id},{profile.id}")

    assert deleted == 3
    assert store.search(scope, "PydanticAI", limit=5) == []
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_atoms_fts WHERE id = ?", (atom.id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_blocks_fts WHERE id = ?", (block.id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_profiles_fts WHERE id = ?", (profile.id,)).fetchone()[0] == 0
        assert conn.execute("SELECT status FROM memory_atoms WHERE id = ?", (atom.id,)).fetchone()["status"] == "deleted"


def test_profile_from_row_preserves_stability(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    profile = MemoryProfile(
        profile_type="user_profile",
        tenant_id=scope.tenant_id,
        user_id=scope.user_id,
        role_name=scope.role_name,
        title="画像",
        summary="短期画像",
        stability=MemoryStability.SITUATIONAL,
    )
    store.add_profile(profile)

    loaded = store.get_active_profile(scope.tenant_id, scope.user_id, scope.role_name, "user_profile")

    assert loaded is not None
    assert loaded.stability == MemoryStability.SITUATIONAL


def test_find_conflict_candidates_finds_old_exact_duplicate(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    old = MemoryAtom(memory_type="preference", content="用户喜欢简洁回答", scope=scope, subject="user", predicate="likes", object="简洁回答")
    store.add_atom(old)
    for index in range(12):
        store.add_atom(MemoryAtom(memory_type="preference", content=f"用户喜欢主题 {index}", scope=scope, subject="user", predicate="likes", object=f"主题 {index}"))
    new = MemoryAtom(memory_type="preference", content="用户喜欢简洁回答", scope=scope, subject="user", predicate="likes", object="简洁回答")

    candidates = store.find_conflict_candidates(scope, new, limit=10)

    assert old.id in {row["id"] for row in candidates}


def test_find_conflict_candidates_filters_different_predicate(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    old = MemoryAtom(memory_type="preference", content="用户喜欢中文", scope=scope, subject="user", predicate="likes", object="中文")
    store.add_atom(old)
    new = MemoryAtom(memory_type="preference", content="用户使用中文", scope=scope, subject="user", predicate="uses", object="中文")

    candidates = store.find_conflict_candidates(scope, new)

    assert old.id not in {row["id"] for row in candidates}
