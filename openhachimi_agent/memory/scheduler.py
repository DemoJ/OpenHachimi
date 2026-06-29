"""长期记忆后台任务调度。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from openhachimi_agent.core.config import AppConfig
from openhachimi_agent.memory.consolidation import consolidate_due_memories
from openhachimi_agent.memory.conflicts import resolve_atom_conflict
from openhachimi_agent.memory.embeddings import EmbeddingProvider
from openhachimi_agent.memory.extraction import extract_memories_from_turn
from openhachimi_agent.memory.models import MemoryAtom, MemoryJob, MemoryScope
from openhachimi_agent.memory.privacy import PrivacyGuard
from openhachimi_agent.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryScheduler:
    def __init__(
        self,
        store: MemoryStore,
        *,
        config: AppConfig | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        poll_interval_seconds: float = 2.0,
        batch_size: int = 10,
    ) -> None:
        self.store = store
        self.config = config
        self.embedding_provider = embedding_provider
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.running = False
        self._task: asyncio.Task[None] | None = None
        # 上次执行 maintenance 的 wall-clock 时间戳;初始化为 0 确保首次 run_once 尽快执行。
        self._last_maintenance_ts: float = 0.0

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.debug("memory scheduler started")

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.debug("memory scheduler stopped")

    async def _run_loop(self) -> None:
        while self.running:
            await self.run_once()
            await asyncio.sleep(self.poll_interval_seconds)

    async def run_once(self) -> dict[str, int]:
        stats = {"claimed": 0, "succeeded": 0, "failed": 0, "atoms_created": 0, "consolidations": 0, "maintenance": 0}
        lock_seconds = self.config.memory.scheduler.lock_seconds if self.config else 300
        jobs = self.store.claim_due_jobs(self.batch_size, lock_seconds=lock_seconds)
        stats["claimed"] = len(jobs)
        for job in jobs:
            try:
                result = await self.handle_job(job)
                stats["atoms_created"] += result.get("atoms_created", 0)
                stats["consolidations"] += result.get("consolidations", 0)
                stats["maintenance"] += result.get("maintenance", 0)
                self.store.complete_job(job.id)
                stats["succeeded"] += 1
            except Exception as exc:  # pragma: no cover - tested via fail_job state, logging keeps scheduler alive
                self.store.fail_job(job.id, str(exc))
                stats["failed"] += 1
                logger.exception("memory job failed job_id=%s type=%s", job.id, job.job_type)
        # 独立 maintenance 定时:不依赖新对话产生的 consolidate job,确保
        # expire_due_atoms / archive_decayed_atoms 定期执行。若 config 未配置
        # 或配置为 0 则不下发(仅通过 maintenance job 手动触发)。
        interval = (self.config.memory.scheduler.maintenance_interval_seconds
                    if self.config else 21600)
        if interval > 0 and time.monotonic() - self._last_maintenance_ts >= interval:
            try:
                self.store.expire_due_atoms()
                self.store.archive_decayed_atoms()
                self._last_maintenance_ts = time.monotonic()
                stats["maintenance"] += 1
            except Exception as exc:
                logger.exception("memory scheduler inline maintenance failed: %s", exc)
        return stats

    async def handle_job(self, job: MemoryJob) -> dict[str, int]:
        if job.job_type == "extract_atoms_from_turn":
            return self._handle_extract_atoms(job.payload)
        if job.job_type == "embed_memory_item":
            return self._handle_embed_memory_item(job.payload)
        if job.job_type == "consolidate_scope":
            if self.config and not self.config.memory.consolidation.enabled:
                return {"consolidations": 0}
            scope = _scope_from_payload(job.payload.get("scope", {}))
            consolidation = self.config.memory.consolidation if self.config else None
            consolidate_due_memories(
                self.store,
                scope=scope,
                atom_limit=consolidation.atom_limit if consolidation else 200,
                min_block_atoms=consolidation.min_block_atoms if consolidation else 2,
                block_limit=consolidation.block_limit if consolidation else 50,
                min_atom_confidence=consolidation.min_atom_confidence if consolidation else 0.55,
                config=self.config,
            )
            return {"consolidations": 1}
        if job.job_type == "maintenance":
            self.store.expire_due_atoms()
            self.store.archive_decayed_atoms()
            return {"maintenance": 1}
        return {}

    def _handle_extract_atoms(self, payload: dict[str, Any]) -> dict[str, int]:
        scope = _scope_from_payload(payload.get("scope", {}))
        guard = PrivacyGuard(
            allow_secret_memory=self.config.memory.privacy.allow_secret_memory if self.config else False,
            pii_redaction=self.config.memory.privacy.pii_redaction if self.config else True,
        )
        result = extract_memories_from_turn(
            str(payload.get("user_message", "")),
            str(payload.get("assistant_output", "")),
            scope,
            str(payload.get("turn_id", "")),
            privacy_guard=guard,
            config=self.config,
        )
        created = 0
        for extracted in result.memories:
            atom = MemoryAtom(
                memory_type=extracted.memory_type,
                content=extracted.content,
                scope=scope,
                subject=extracted.subject,
                predicate=extracted.predicate,
                object=extracted.object,
                evidence_turn_ids=[str(payload.get("turn_id", ""))],
                source_quote=extracted.source_quote,
                entities=extracted.entities,
                keywords=extracted.keywords,
                tags=extracted.tags,
                confidence=extracted.confidence,
                stability=extracted.stability,
                sensitivity=extracted.sensitivity,
                valid_until=getattr(extracted, "valid_until", None),
                decay_at=getattr(extracted, "decay_at", None),
            )
            embedding = None
            if self.config and self.config.memory.embedding.enabled:
                provider = self.embedding_provider or EmbeddingProvider(self.config.memory.embedding)
                embedding = provider.embed_sync(atom.content)
            decision = resolve_atom_conflict(
                self.store,
                atom,
                embedding_vector=embedding.vector if embedding and not embedding.degraded else None,
                embedding_model=self.config.memory.embedding.model if self.config and embedding and not embedding.degraded else None,
            )
            if decision.action == "dedupe":
                continue
            atom_id = self.store.add_atom(atom)
            if decision.action == "supersede" and decision.loser_id:
                self.store.mark_atom_superseded(decision.loser_id, atom_id, decision.conflict_group_id)
                self.store.record_conflict(scope, decision.conflict_key, atom_id, decision.loser_id, decision.reason)
            created += 1
            if self.config and self.config.memory.embedding.enabled:
                if embedding and not embedding.degraded:
                    self.store.save_vector(atom_id, "L1", self.config.memory.embedding.model, embedding.vector)
                else:
                    self.store.enqueue_unique_job(
                        "embed_memory_item",
                        {"item_id": atom_id, "level": "L1", "text": atom.content, "model": self.config.memory.embedding.model},
                        dedupe_key=f"embed:L1:{atom_id}",
                    )
        if created:
            self.store.enqueue_unique_job("consolidate_scope", {"scope": scope.to_json_dict()}, dedupe_key=f"consolidate:{scope.tenant_id}:{scope.user_id}:{scope.role_name}")
        return {"atoms_created": created}

    def _handle_embed_memory_item(self, payload: dict[str, Any]) -> dict[str, int]:
        if not self.config:
            return {}
        provider = self.embedding_provider or EmbeddingProvider(self.config.memory.embedding)
        item_id = str(payload.get("item_id", ""))
        level = str(payload.get("level", "L1"))
        text = str(payload.get("text", ""))
        embedding = provider.embed_sync(text)
        if embedding.degraded:
            if level == "L1":
                self.store.set_atom_embedding_status(item_id, "failed")
            elif level == "L2":
                self.store.set_block_embedding_status(item_id, "failed")
            raise RuntimeError(embedding.reason)
        self.store.save_vector(item_id, level, str(payload.get("model") or self.config.memory.embedding.model), embedding.vector)
        return {}


def _scope_from_payload(payload: dict[str, Any]) -> MemoryScope:
    return MemoryScope(
        tenant_id=str(payload.get("tenant_id", "local")),
        user_id=str(payload.get("user_id", "local")),
        role_name=str(payload.get("role_name", "default")),
        session_id=str(payload.get("session_id", "")),
        channel=str(payload.get("channel", "cli")),
    )
