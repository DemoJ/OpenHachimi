from dataclasses import replace

import asyncio

import pytest

from openhachimi_agent.core.config import MemoryConsolidationConfig, MemorySchedulerConfig
from openhachimi_agent.memory.llm import MemoryExtractionOutput, MemoryLLMItem
from openhachimi_agent.memory.models import MemoryJob, MemoryScope
from openhachimi_agent.memory.scheduler import MemoryScheduler
from openhachimi_agent.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_scheduler_extracts_atoms_from_turn_job(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    store.enqueue_job(
        MemoryJob(
            job_type="extract_atoms_from_turn",
            payload={"turn_id": "turn1", "scope": scope.to_json_dict(), "user_message": "请记住：以后回答使用中文。", "assistant_output": "好的"},
        )
    )
    scheduler = MemoryScheduler(store, batch_size=5)

    stats = await scheduler.run_once()

    assert stats["succeeded"] == 1
    assert store.stats()["atoms"] == 1


@pytest.mark.asyncio
async def test_scheduler_runs_consolidation_job(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    scheduler = MemoryScheduler(store, batch_size=10)
    store.enqueue_job(
        MemoryJob(
            job_type="extract_atoms_from_turn",
            payload={"turn_id": "turn1", "scope": scope.to_json_dict(), "user_message": "请记住：以后回答使用中文。", "assistant_output": "好的"},
        )
    )

    await scheduler.run_once()
    await scheduler.run_once()

    assert store.stats()["profiles"] >= 1


@pytest.mark.asyncio
async def test_scheduler_passes_lock_seconds(tmp_path, mock_config, monkeypatch):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    config = replace(mock_config, memory=replace(mock_config.memory, scheduler=MemorySchedulerConfig(lock_seconds=7, batch_size=3)))
    scheduler = MemoryScheduler(store, config=config, batch_size=3)
    captured = {}

    def fake_claim(limit, *, lock_seconds=300):
        captured["limit"] = limit
        captured["lock_seconds"] = lock_seconds
        return []

    monkeypatch.setattr(store, "claim_due_jobs", fake_claim)

    await scheduler.run_once()

    assert captured == {"limit": 3, "lock_seconds": 7}


@pytest.mark.asyncio
async def test_scheduler_skips_consolidation_when_disabled(tmp_path, mock_config, monkeypatch):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    config = replace(mock_config, memory=replace(mock_config.memory, consolidation=MemoryConsolidationConfig(enabled=False)))
    scheduler = MemoryScheduler(store, config=config)
    called = False

    def fake_consolidate(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("openhachimi_agent.memory.scheduler.consolidate_due_memories", fake_consolidate)

    result = await scheduler.handle_job(MemoryJob(job_type="consolidate_scope", payload={"scope": scope.to_json_dict()}))

    assert result == {"consolidations": 0}
    assert called is False


@pytest.mark.asyncio
async def test_scheduler_passes_consolidation_config(tmp_path, mock_config, monkeypatch):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    consolidation = MemoryConsolidationConfig(atom_limit=7, block_limit=3, min_atom_confidence=0.8, min_block_atoms=4)
    config = replace(mock_config, memory=replace(mock_config.memory, consolidation=consolidation))
    scheduler = MemoryScheduler(store, config=config)
    captured = {}

    def fake_consolidate(*args, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr("openhachimi_agent.memory.scheduler.consolidate_due_memories", fake_consolidate)

    result = await scheduler.handle_job(MemoryJob(job_type="consolidate_scope", payload={"scope": scope.to_json_dict()}))

    assert result == {"consolidations": 1}
    assert captured["atom_limit"] == 7
    assert captured["block_limit"] == 3
    assert captured["min_atom_confidence"] == 0.8
    assert captured["min_block_atoms"] == 4


@pytest.mark.asyncio
async def test_scheduler_runs_extraction_off_event_loop(tmp_path, mock_config, monkeypatch):
    """回归:调度器执行 LLM 抽取时,回调必须跑在没有运行中事件循环的工作线程里。

    旧版 ``handle_job`` 在主事件循环里同步调用 ``extract_memories_from_turn``,
    导致其内部的 ``asyncio.run`` 抛 ``RuntimeError``,``agent.run`` 协程随之泄漏,
    触发 ``coroutine 'AbstractAgent.run' was never awaited`` 警告,LLM 通道静默降级。
    用 ``asyncio.to_thread`` 包裹后,回调应落在无循环的工作线程,``asyncio.run`` 可正常工作。
    """
    store = MemoryStore(tmp_path / "memory.sqlite3")
    scope = MemoryScope(role_name="default", session_id="s1")
    config = replace(mock_config, openai_api_key="key", openai_base_url="https://llm.example/v1")
    scheduler = MemoryScheduler(store, config=config, batch_size=5)
    captured: dict[str, object] = {}

    def fake_run_memory_extraction(config_arg, *, system_prompt, payload):
        # to_thread 生效时,回调跑在无运行中事件循环的工作线程 -> running_loop 应抛异常。
        try:
            asyncio.get_running_loop()
            captured["has_running_loop"] = True
        except RuntimeError:
            captured["has_running_loop"] = False
        return MemoryExtractionOutput(
            memories=[
                MemoryLLMItem(
                    memory_type="preference",
                    content="user prefers concise answers",
                    confidence=0.9,
                    stability="stable",
                )
            ]
        )

    monkeypatch.setattr("openhachimi_agent.memory.extraction.run_memory_extraction", fake_run_memory_extraction)

    store.enqueue_job(
        MemoryJob(
            job_type="extract_atoms_from_turn",
            payload={"turn_id": "turn1", "scope": scope.to_json_dict(), "user_message": "请记住：以后回答保持简洁。", "assistant_output": "好的"},
        )
    )

    stats = await scheduler.run_once()

    assert stats["succeeded"] == 1
    assert captured["has_running_loop"] is False
