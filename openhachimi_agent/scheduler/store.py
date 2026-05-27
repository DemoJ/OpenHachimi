"""定时任务 SQLite 存储。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from openhachimi_agent.scheduler.models import RunStatus, ScheduledRun, ScheduledTask, ScheduleType, utc_now
from openhachimi_agent.scheduler.time_utils import compute_next_run


def _dt_to_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt_from_text(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class ScheduledTaskStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    schedule_expr TEXT NOT NULL,
                    role TEXT,
                    session_id TEXT,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT,
                    timeout_seconds INTEGER,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT,
                    last_status TEXT,
                    last_error TEXT,
                    running INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT
                );

                CREATE TABLE IF NOT EXISTS scheduled_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    output TEXT,
                    error TEXT,
                    duration_ms INTEGER,
                    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
                    ON scheduled_tasks(enabled, next_run_at, running, locked_until);
                CREATE INDEX IF NOT EXISTS idx_scheduled_runs_task_started
                    ON scheduled_runs(task_id, started_at DESC);
                """
            )

    def create_task(
        self,
        *,
        name: str,
        prompt: str,
        schedule_type: ScheduleType | str,
        schedule_expr: str,
        role: str | None = None,
        session_id: str | None = None,
        timezone_name: str = "UTC",
        enabled: bool = True,
        timeout_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ScheduledTask:
        now = utc_now()
        kind = ScheduleType(schedule_type)
        next_run_at = compute_next_run(kind, schedule_expr, after=now, timezone_name=timezone_name) if enabled else None
        task_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, name, prompt, schedule_type, schedule_expr, role, session_id, timezone,
                    enabled, next_run_at, timeout_seconds, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    name.strip(),
                    prompt,
                    kind.value,
                    schedule_expr.strip(),
                    role,
                    session_id,
                    timezone_name,
                    1 if enabled else 0,
                    _dt_to_text(next_run_at),
                    timeout_seconds,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    _dt_to_text(now),
                    _dt_to_text(now),
                ),
            )
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError("定时任务创建失败。")
        return task

    def list_tasks(self) -> list[ScheduledTask]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC").fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_task(self, task_id: str) -> ScheduledTask | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def update_task(self, task_id: str, **updates: Any) -> ScheduledTask:
        allowed = {"name", "prompt", "schedule_type", "schedule_expr", "role", "session_id", "timezone", "enabled", "timeout_seconds", "metadata"}
        current = self.get_task(task_id)
        if current is None:
            raise KeyError(task_id)
        data = {key: value for key, value in updates.items() if key in allowed}
        if not data:
            return current
        schedule_type = ScheduleType(data.get("schedule_type", current.schedule_type))
        schedule_expr = str(data.get("schedule_expr", current.schedule_expr))
        timezone_name = str(data.get("timezone", current.timezone))
        enabled = bool(data.get("enabled", current.enabled))
        schedule_changed = any(key in data for key in {"schedule_type", "schedule_expr", "timezone", "enabled"})
        next_run_at = current.next_run_at
        if schedule_changed:
            next_run_at = compute_next_run(schedule_type, schedule_expr, after=utc_now(), timezone_name=timezone_name) if enabled else None
        assignments = []
        values: list[Any] = []
        for key, value in data.items():
            column_value = value
            if key == "schedule_type":
                column_value = ScheduleType(value).value
            elif key == "enabled":
                column_value = 1 if value else 0
            elif key == "metadata":
                column_value = json.dumps(value or {}, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            values.append(column_value)
        assignments.extend(["next_run_at = ?", "updated_at = ?"])
        values.extend([_dt_to_text(next_run_at), _dt_to_text(utc_now()), task_id])
        with self._connect() as conn:
            conn.execute(f"UPDATE scheduled_tasks SET {', '.join(assignments)} WHERE id = ?", values)
        updated = self.get_task(task_id)
        if updated is None:
            raise KeyError(task_id)
        return updated

    def delete_task(self, task_id: str) -> None:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        if cur.rowcount == 0:
            raise KeyError(task_id)

    def claim_due_tasks(self, limit: int, lock_seconds: int) -> list[ScheduledTask]:
        now = utc_now()
        locked_until = now + timedelta(seconds=lock_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE enabled = 1
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= ?
                  AND (running = 0 OR locked_until IS NULL OR locked_until <= ?)
                ORDER BY next_run_at ASC
                LIMIT ?
                """,
                (_dt_to_text(now), _dt_to_text(now), limit),
            ).fetchall()
            task_ids = [row["id"] for row in rows]
            claimed_rows = []
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                conn.execute(
                    f"""
                    UPDATE scheduled_tasks
                    SET running = 1, locked_until = ?, updated_at = ?
                    WHERE id IN ({placeholders})
                      AND enabled = 1
                      AND next_run_at IS NOT NULL
                      AND next_run_at <= ?
                      AND (running = 0 OR locked_until IS NULL OR locked_until <= ?)
                    """,
                    [_dt_to_text(locked_until), _dt_to_text(now), *task_ids, _dt_to_text(now), _dt_to_text(now)],
                )
                claimed_rows = conn.execute(
                    f"SELECT * FROM scheduled_tasks WHERE id IN ({placeholders}) AND running = 1 AND locked_until = ? ORDER BY next_run_at ASC",
                    [*task_ids, _dt_to_text(locked_until)],
                ).fetchall()
        return [self._task_from_row(row) for row in claimed_rows]

    def claim_task_now(self, task_id: str, lock_seconds: int) -> ScheduledTask:
        now = utc_now()
        locked_until = now + timedelta(seconds=lock_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = self._task_from_row(row)
            if current.running and current.locked_until and current.locked_until > now:
                raise RuntimeError("定时任务正在运行")
            cur = conn.execute(
                """
                UPDATE scheduled_tasks
                SET running = 1, locked_until = ?, updated_at = ?
                WHERE id = ?
                  AND (running = 0 OR locked_until IS NULL OR locked_until <= ?)
                """,
                (_dt_to_text(locked_until), _dt_to_text(now), task_id, _dt_to_text(now)),
            )
            if cur.rowcount == 0:
                raise RuntimeError("定时任务正在运行")
        claimed = self.get_task(task_id)
        if claimed is None:
            raise KeyError(task_id)
        return claimed

    def prepare_task_run(self, task: ScheduledTask, *, preserve_schedule: bool = False) -> ScheduledRun:
        now = utc_now()
        next_run_at = task.next_run_at if preserve_schedule else None
        enabled = task.enabled if preserve_schedule else False
        if task.schedule_type != ScheduleType.ONCE and not preserve_schedule:
            next_run_at = compute_next_run(task.schedule_type, task.schedule_expr, after=now, timezone_name=task.timezone)
            enabled = task.enabled
        run = ScheduledRun(id=uuid.uuid4().hex, task_id=task.id, status="running", started_at=now)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET next_run_at = ?, enabled = ?, last_run_at = ?, last_status = ?, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_dt_to_text(next_run_at), 1 if enabled else 0, _dt_to_text(now), "running", _dt_to_text(now), task.id),
            )
            conn.execute(
                "INSERT INTO scheduled_runs (id, task_id, status, started_at) VALUES (?, ?, ?, ?)",
                (run.id, run.task_id, run.status, _dt_to_text(run.started_at)),
            )
        return run

    def complete_run(self, run_id: str, *, status: RunStatus, output: str | None = None, error: str | None = None, duration_ms: int | None = None) -> None:
        finished_at = utc_now()
        with self._connect() as conn:
            row = conn.execute("SELECT task_id FROM scheduled_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            task_id = row["task_id"]
            conn.execute(
                """
                UPDATE scheduled_runs
                SET status = ?, finished_at = ?, output = ?, error = ?, duration_ms = ?
                WHERE id = ?
                """,
                (status, _dt_to_text(finished_at), output, error, duration_ms, run_id),
            )
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET running = 0, locked_until = NULL, last_status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, _dt_to_text(finished_at), task_id),
            )

    def skip_task_run(self, task: ScheduledTask, *, error: str | None = None) -> ScheduledRun:
        now = utc_now()
        next_run_at = None
        enabled = False
        if task.schedule_type != ScheduleType.ONCE:
            next_run_at = compute_next_run(task.schedule_type, task.schedule_expr, after=now, timezone_name=task.timezone)
            enabled = task.enabled
        run_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET running = 0, locked_until = NULL, next_run_at = ?, enabled = ?, last_run_at = ?, last_status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (_dt_to_text(next_run_at), 1 if enabled else 0, _dt_to_text(now), "skipped", error, _dt_to_text(now), task.id),
            )
            conn.execute(
                """
                INSERT INTO scheduled_runs (id, task_id, status, started_at, finished_at, error, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, task.id, "skipped", _dt_to_text(now), _dt_to_text(now), error, 0),
            )
        return ScheduledRun(id=run_id, task_id=task.id, status="skipped", started_at=now, finished_at=now, error=error, duration_ms=0)

    def release_task(self, task_id: str, *, status: RunStatus = "skipped", error: str | None = None) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET running = 0, locked_until = NULL, last_status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, _dt_to_text(now), task_id),
            )

    def get_run(self, run_id: str) -> ScheduledRun | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_from_row(row) if row else None

    def list_runs(self, task_id: str, limit: int = 20) -> list[ScheduledRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_runs WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def _task_from_row(self, row: sqlite3.Row) -> ScheduledTask:
        return ScheduledTask(
            id=row["id"],
            name=row["name"],
            prompt=row["prompt"],
            schedule_type=ScheduleType(row["schedule_type"]),
            schedule_expr=row["schedule_expr"],
            role=row["role"],
            session_id=row["session_id"],
            timezone=row["timezone"],
            enabled=bool(row["enabled"]),
            next_run_at=_dt_from_text(row["next_run_at"]),
            timeout_seconds=row["timeout_seconds"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=_dt_from_text(row["created_at"]) or utc_now(),
            updated_at=_dt_from_text(row["updated_at"]) or utc_now(),
            last_run_at=_dt_from_text(row["last_run_at"]),
            last_status=row["last_status"],
            last_error=row["last_error"],
            running=bool(row["running"]),
            locked_until=_dt_from_text(row["locked_until"]),
        )

    def _run_from_row(self, row: sqlite3.Row) -> ScheduledRun:
        return ScheduledRun(
            id=row["id"],
            task_id=row["task_id"],
            status=row["status"],
            started_at=_dt_from_text(row["started_at"]) or utc_now(),
            finished_at=_dt_from_text(row["finished_at"]),
            output=row["output"],
            error=row["error"],
            duration_ms=row["duration_ms"],
        )
