import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from openhachimi_agent.memory.models import (
    MemoryJob,
    MemoryJobStatus,
    utc_now_iso,
)
from openhachimi_agent.memory._store.utils import (
    _job_from_row,
    _json,
    _load_json_dict,
)


class JobQueueStoreMixin:
    def enqueue_job(self, job: MemoryJob) -> str:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_jobs(
                    id, job_type, payload_json, status, attempts, max_attempts, run_after,
                    locked_at, last_error, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.id,
                    job.job_type,
                    _json(job.payload),
                    str(job.status),
                    job.attempts,
                    job.max_attempts,
                    job.run_after,
                    job.locked_at,
                    job.last_error,
                    job.created_at,
                    job.updated_at,
                ),
            )
        return job.id

    def enqueue_unique_job(self, job_type: str, payload: dict[str, Any], *, dedupe_key: str, run_after: str | None = None) -> str:
        with self.connect() as conn:
            try:
                row = conn.execute(
                    """
                    SELECT id FROM memory_jobs
                    WHERE job_type = ? AND status IN (?, ?, ?)
                      AND json_extract(payload_json, '$._dedupe_key') = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (job_type, MemoryJobStatus.PENDING.value, MemoryJobStatus.RETRY.value, MemoryJobStatus.RUNNING.value, dedupe_key),
                ).fetchone()
            except sqlite3.OperationalError:
                rows = conn.execute(
                    """
                    SELECT id, payload_json FROM memory_jobs
                    WHERE job_type = ? AND status IN (?, ?, ?)
                    ORDER BY created_at DESC
                    """,
                    (job_type, MemoryJobStatus.PENDING.value, MemoryJobStatus.RETRY.value, MemoryJobStatus.RUNNING.value),
                ).fetchall()
                row = next((item for item in rows if _load_json_dict(item["payload_json"]).get("_dedupe_key") == dedupe_key), None)
            if row:
                return str(row["id"])
        full_payload = dict(payload)
        full_payload["_dedupe_key"] = dedupe_key
        return self.enqueue_job(MemoryJob(job_type=job_type, payload=full_payload, run_after=run_after or utc_now_iso()))

    def claim_due_jobs(self, limit: int = 10, *, lock_seconds: int = 300) -> list[MemoryJob]:
        now = utc_now_iso()
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=lock_seconds)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                UPDATE memory_jobs
                SET status = ?, attempts = attempts + 1, locked_at = ?, updated_at = ?
                WHERE id IN (
                    SELECT id FROM memory_jobs
                    WHERE (status IN (?, ?) AND run_after <= ?)
                       OR (status = ? AND locked_at IS NOT NULL AND locked_at <= ?)
                    ORDER BY run_after, created_at
                    LIMIT ?
                )
                RETURNING *
                """,
                (
                    MemoryJobStatus.RUNNING.value,
                    now,
                    now,
                    MemoryJobStatus.PENDING.value,
                    MemoryJobStatus.RETRY.value,
                    now,
                    MemoryJobStatus.RUNNING.value,
                    stale_before,
                    limit,
                ),
            ).fetchall()
            return [_job_from_row(row) for row in rows]

    def complete_job(self, job_id: str) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE memory_jobs SET status = ?, locked_at = NULL, last_error = '', updated_at = ? WHERE id = ?",
                (MemoryJobStatus.SUCCEEDED.value, now, job_id),
            )

    def fail_job(self, job_id: str, error: str, *, retry_delay_seconds: int = 60) -> None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self.connect() as conn:
            row = conn.execute("SELECT attempts, max_attempts FROM memory_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return
            if int(row["attempts"]) < int(row["max_attempts"]):
                status = MemoryJobStatus.RETRY.value
                run_after = (now_dt + timedelta(seconds=retry_delay_seconds * max(1, int(row["attempts"])))).isoformat()
            else:
                status = MemoryJobStatus.FAILED.value
                run_after = now
            conn.execute(
                """
                UPDATE memory_jobs
                SET status = ?, run_after = ?, locked_at = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, run_after, error[:1000], now, job_id),
            )
