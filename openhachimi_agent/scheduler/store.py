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

SQLITE_BUSY_TIMEOUT_SECONDS = 30


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


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


def _json_list(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    loaded = json.loads(value)
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


def _task_status(status: str | None) -> str:
    return status if status in {"enabled", "paused", "deleted"} else "enabled"


class ScheduledTaskStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
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
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    status TEXT NOT NULL DEFAULT 'enabled',
                    role TEXT,
                    session_id TEXT,
                    timeout_seconds INTEGER,
                    origin TEXT NOT NULL DEFAULT '{}',
                    delivery_mode TEXT NOT NULL DEFAULT 'origin',
                    delivery_targets TEXT NOT NULL DEFAULT '[]',
                    delivery_fallback TEXT NOT NULL DEFAULT '{}',
                    execution_policy TEXT NOT NULL DEFAULT '{}',
                    safety_status TEXT,
                    safety_error TEXT,
                    next_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT,
                    last_status TEXT,
                    last_error TEXT,
                    last_delivery_status TEXT,
                    last_delivery_error TEXT,
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
                    delivery_status TEXT,
                    delivery_targets TEXT NOT NULL DEFAULT '[]',
                    delivery_results TEXT NOT NULL DEFAULT '[]',
                    delivery_error TEXT,
                    delivered_at TEXT,
                    read_at TEXT,
                    safety_status TEXT,
                    safety_error TEXT,
                    execution_context TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
                    ON scheduled_tasks(status, next_run_at, running, locked_until);
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
        timezone_name: str = "UTC",
        status: str = "enabled",
        role: str | None = None,
        session_id: str | None = None,
        timeout_seconds: int | None = None,
        origin: dict[str, Any] | None = None,
        delivery_mode: str = "origin",
        delivery_targets: list[dict[str, Any]] | None = None,
        delivery_fallback: dict[str, Any] | None = None,
        execution_policy: dict[str, Any] | None = None,
    ) -> ScheduledTask:
        now = utc_now()
        kind = ScheduleType(schedule_type)
        normalized_status = _task_status(status)
        next_run_at = compute_next_run(kind, schedule_expr, after=now, timezone_name=timezone_name) if normalized_status == "enabled" else None
        task_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, name, prompt, schedule_type, schedule_expr, timezone, status,
                    role, session_id, timeout_seconds, origin, delivery_mode, delivery_targets,
                    delivery_fallback, execution_policy, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    name.strip(),
                    prompt,
                    kind.value,
                    schedule_expr.strip(),
                    timezone_name,
                    normalized_status,
                    role,
                    session_id,
                    timeout_seconds,
                    _json_dumps(origin or {}),
                    delivery_mode,
                    _json_dumps(delivery_targets or []),
                    _json_dumps(delivery_fallback or {}),
                    _json_dumps(execution_policy or {}),
                    _dt_to_text(next_run_at),
                    _dt_to_text(now),
                    _dt_to_text(now),
                ),
            )
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError("定时任务创建失败。")
        return task

    def list_tasks(self, *, include_deleted: bool = False) -> list[ScheduledTask]:
        with self._connect() as conn:
            if include_deleted:
                rows = conn.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM scheduled_tasks WHERE status != 'deleted' ORDER BY created_at DESC").fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_task(self, task_id: str) -> ScheduledTask | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def resolve_task_ref(self, task_id_or_name: str, *, include_deleted: bool = False) -> ScheduledTask | None:
        task = self.get_task(task_id_or_name)
        if task is not None and (include_deleted or task.status != "deleted"):
            return task
        matches = [task for task in self.list_tasks(include_deleted=include_deleted) if task.name == task_id_or_name]
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"找到多个同名定时任务：{task_id_or_name}，请使用任务 ID。")
        return matches[0]

    def update_task(self, task_id: str, **updates: Any) -> ScheduledTask:
        allowed = {
            "name",
            "prompt",
            "schedule_type",
            "schedule_expr",
            "timezone",
            "status",
            "role",
            "session_id",
            "timeout_seconds",
            "origin",
            "delivery_mode",
            "delivery_targets",
            "delivery_fallback",
            "execution_policy",
            "safety_status",
            "safety_error",
            "last_error",
            "last_delivery_status",
            "last_delivery_error",
        }
        current = self.get_task(task_id)
        if current is None:
            raise KeyError(task_id)
        data = {key: value for key, value in updates.items() if key in allowed}
        if not data:
            return current

        schedule_type = ScheduleType(data.get("schedule_type", current.schedule_type))
        schedule_expr = str(data.get("schedule_expr", current.schedule_expr))
        timezone_name = str(data.get("timezone", current.timezone))
        status = _task_status(str(data.get("status", current.status)))
        schedule_changed = any(key in data for key in {"schedule_type", "schedule_expr", "timezone", "status"})
        next_run_at = current.next_run_at
        if schedule_changed:
            next_run_at = compute_next_run(schedule_type, schedule_expr, after=utc_now(), timezone_name=timezone_name) if status == "enabled" else None

        assignments = []
        values: list[Any] = []
        for key, value in data.items():
            column_value = value
            if key == "schedule_type":
                column_value = ScheduleType(value).value
            elif key == "status":
                column_value = _task_status(str(value))
            elif key in {"origin", "delivery_targets", "delivery_fallback", "execution_policy"}:
                column_value = _json_dumps(value or ([] if key == "delivery_targets" else {}))
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

    def pause_task(self, task_id: str, *, reason: str | None = None) -> ScheduledTask:
        updates: dict[str, Any] = {"status": "paused"}
        if reason:
            updates["last_error"] = reason
        return self.update_task(task_id, **updates)

    def resume_task(self, task_id: str) -> ScheduledTask:
        return self.update_task(task_id, status="enabled")

    def delete_task(self, task_id: str) -> ScheduledTask:
        return self.update_task(task_id, status="deleted")

    def claim_due_tasks(self, limit: int, lock_seconds: int) -> list[ScheduledTask]:
        now = utc_now()
        locked_until = now + timedelta(seconds=lock_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._recover_stale_running_tasks(conn, now)
            rows = conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = 'enabled'
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
                      AND status = 'enabled'
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

    def _recover_stale_running_tasks(self, conn: sqlite3.Connection, now: datetime) -> None:
        """Recover tasks that were claimed but never completed (process crash).

        - ONCE tasks: mark deleted + skipped (already attempted, don't retry)
        - Recurring tasks: reset to enabled, recompute next_run_at, mark run skipped
        """
        rows = conn.execute(
            """
            SELECT t.*
            FROM scheduled_tasks t
            WHERE t.running = 1
              AND t.locked_until IS NOT NULL
              AND t.locked_until <= ?
              AND EXISTS (
                  SELECT 1 FROM scheduled_runs r
                  WHERE r.task_id = t.id AND r.status = 'running'
              )
            """,
            (_dt_to_text(now),),
        ).fetchall()
        for row in rows:
            task = self._task_from_row(row)
            if task.schedule_type == ScheduleType.ONCE:
                # ONCE: already attempted, mark deleted to prevent re-execution
                conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET status = 'deleted',
                        running = 0,
                        locked_until = NULL,
                        next_run_at = NULL,
                        last_status = 'skipped',
                        last_error = 'stale running task lock recovered (once)',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (_dt_to_text(now), task.id),
                )
            else:
                next_run_at = task.next_run_at or compute_next_run(
                    task.schedule_type, task.schedule_expr, after=now, timezone_name=task.timezone
                )
                conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET running = 0,
                        locked_until = NULL,
                        next_run_at = ?,
                        last_status = 'skipped',
                        last_error = 'stale running task lock recovered',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (_dt_to_text(next_run_at), _dt_to_text(now), task.id),
                )
            conn.execute(
                """
                UPDATE scheduled_runs
                SET status = 'skipped',
                    finished_at = ?,
                    error = 'stale running task lock recovered',
                    duration_ms = 0
                WHERE task_id = ? AND status = 'running'
                """,
                (_dt_to_text(now), task.id),
            )

    def claim_task_now(self, task_id: str, lock_seconds: int) -> ScheduledTask:
        now = utc_now()
        locked_until = now + timedelta(seconds=lock_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = self._task_from_row(row)
            if current.status == "deleted":
                raise RuntimeError("定时任务已删除")
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

    def prepare_task_run(self, task: ScheduledTask, *, preserve_schedule: bool = False, execution_context: dict[str, Any] | None = None) -> ScheduledRun:
        now = utc_now()
        # 仅推进周期任务的 next_run_at；ONCE 任务保持 next_run_at 不变，
        # 状态变更延迟到 complete_run，避免 prepare 与 complete 之间崩溃导致
        # ONCE 任务变成 deleted+running=1 永远无法恢复。
        if preserve_schedule:
            next_run_at = task.next_run_at
        elif task.schedule_type != ScheduleType.ONCE:
            next_run_at = compute_next_run(task.schedule_type, task.schedule_expr, after=now, timezone_name=task.timezone)
        else:
            next_run_at = task.next_run_at  # ONCE: 不动，等 complete_run 标记 deleted
        merged_context = dict(execution_context or {})
        merged_context["_preserve_schedule"] = preserve_schedule
        merged_context["_schedule_type"] = task.schedule_type.value
        run = ScheduledRun(id=uuid.uuid4().hex, task_id=task.id, status="running", started_at=now, execution_context=merged_context)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET next_run_at = ?, last_run_at = ?, last_status = ?, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_dt_to_text(next_run_at), _dt_to_text(now), "running", _dt_to_text(now), task.id),
            )
            conn.execute(
                """
                INSERT INTO scheduled_runs (id, task_id, status, started_at, execution_context)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run.id, run.task_id, run.status, _dt_to_text(run.started_at), _json_dumps(run.execution_context)),
            )
        return run

    def complete_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        output: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        safety_status: str | None = None,
        safety_error: str | None = None,
    ) -> None:
        finished_at = utc_now()
        with self._connect() as conn:
            row = conn.execute("SELECT task_id, execution_context FROM scheduled_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            task_id = row["task_id"]
            exec_ctx = _json_dict(row["execution_context"])
            preserve_schedule = exec_ctx.get("_preserve_schedule", False)
            schedule_type = exec_ctx.get("_schedule_type")
            conn.execute(
                """
                UPDATE scheduled_runs
                SET status = ?, finished_at = ?, output = ?, error = ?, duration_ms = ?, safety_status = ?, safety_error = ?
                WHERE id = ?
                """,
                (status, _dt_to_text(finished_at), output, error, duration_ms, safety_status, safety_error, run_id),
            )
            # ONCE 任务在非 preserve_schedule 模式下完成后标记为 deleted
            should_delete = (
                schedule_type == ScheduleType.ONCE.value
                and not preserve_schedule
            )
            if should_delete:
                conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET status = 'deleted',
                        running = 0,
                        locked_until = NULL,
                        next_run_at = NULL,
                        last_status = ?,
                        last_error = ?,
                        safety_status = ?,
                        safety_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error, safety_status, safety_error, _dt_to_text(finished_at), task_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET running = 0, locked_until = NULL, last_status = ?, last_error = ?, safety_status = ?, safety_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error, safety_status, safety_error, _dt_to_text(finished_at), task_id),
                )

    def skip_task_run(self, task: ScheduledTask, *, error: str | None = None) -> ScheduledRun:
        claimed = self.prepare_task_run(task)
        self.complete_run(claimed.id, status="skipped", error=error, duration_ms=0)
        run = self.get_run(claimed.id)
        if run is None:
            raise KeyError(claimed.id)
        return run

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

    def update_run_delivery(
        self,
        run_id: str,
        *,
        delivery_status: str,
        delivery_targets: list[dict[str, Any]] | None = None,
        delivery_results: list[dict[str, Any]] | None = None,
        delivery_error: str | None = None,
        delivered_at: datetime | None = None,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT task_id FROM scheduled_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            task_id = row["task_id"]
            conn.execute(
                """
                UPDATE scheduled_runs
                SET delivery_status = ?, delivery_targets = ?, delivery_results = ?, delivery_error = ?, delivered_at = ?
                WHERE id = ?
                """,
                (
                    delivery_status,
                    _json_dumps(delivery_targets or []),
                    _json_dumps(delivery_results or []),
                    delivery_error,
                    _dt_to_text(delivered_at),
                    run_id,
                ),
            )
            conn.execute(
                """
                UPDATE scheduled_tasks
                SET last_delivery_status = ?, last_delivery_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (delivery_status, delivery_error, _dt_to_text(utc_now()), task_id),
            )

    def update_run_safety(self, run_id: str, *, safety_status: str | None, safety_error: str | None) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE scheduled_runs SET safety_status = ?, safety_error = ? WHERE id = ?",
                (safety_status, safety_error, run_id),
            )
        if cur.rowcount == 0:
            raise KeyError(run_id)

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

    def list_inbox_runs(self, *, unread_only: bool = True, limit: int = 20) -> list[tuple[ScheduledTask, ScheduledRun]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_runs
                WHERE finished_at IS NOT NULL
                  AND (output IS NOT NULL OR error IS NOT NULL)
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (max(limit * 10, 50),),
            ).fetchall()
        results: list[tuple[ScheduledTask, ScheduledRun]] = []
        for row in rows:
            run = self._run_from_row(row)
            if unread_only and run.read_at is not None:
                continue
            if not _run_has_inbox(run):
                continue
            task = self.get_task(run.task_id)
            if task is None:
                continue
            results.append((task, run))
            if len(results) >= limit:
                break
        return results

    def mark_run_read(self, run_id: str) -> None:
        with self._connect() as conn:
            cur = conn.execute("UPDATE scheduled_runs SET read_at = ? WHERE id = ?", (_dt_to_text(utc_now()), run_id))
        if cur.rowcount == 0:
            raise KeyError(run_id)

    def _task_from_row(self, row: sqlite3.Row) -> ScheduledTask:
        return ScheduledTask(
            id=row["id"],
            name=row["name"],
            prompt=row["prompt"],
            schedule_type=ScheduleType(row["schedule_type"]),
            schedule_expr=row["schedule_expr"],
            timezone=row["timezone"],
            status=_task_status(row["status"]),
            role=row["role"],
            session_id=row["session_id"],
            timeout_seconds=row["timeout_seconds"],
            origin=_json_dict(row["origin"]),
            delivery_mode=row["delivery_mode"],
            delivery_targets=_json_list(row["delivery_targets"]),
            delivery_fallback=_json_dict(row["delivery_fallback"]),
            execution_policy=_json_dict(row["execution_policy"]),
            safety_status=row["safety_status"],
            safety_error=row["safety_error"],
            next_run_at=_dt_from_text(row["next_run_at"]),
            created_at=_dt_from_text(row["created_at"]) or utc_now(),
            updated_at=_dt_from_text(row["updated_at"]) or utc_now(),
            last_run_at=_dt_from_text(row["last_run_at"]),
            last_status=row["last_status"],
            last_error=row["last_error"],
            last_delivery_status=row["last_delivery_status"],
            last_delivery_error=row["last_delivery_error"],
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
            delivery_status=row["delivery_status"],
            delivery_targets=_json_list(row["delivery_targets"]),
            delivery_results=_json_list(row["delivery_results"]),
            delivery_error=row["delivery_error"],
            delivered_at=_dt_from_text(row["delivered_at"]),
            read_at=_dt_from_text(row["read_at"]),
            safety_status=row["safety_status"],
            safety_error=row["safety_error"],
            execution_context=_json_dict(row["execution_context"]),
        )


def _run_has_inbox(run: ScheduledRun) -> bool:
    targets = run.delivery_targets or []
    results = run.delivery_results or []
    return any(target.get("type") == "inbox" for target in targets) or any(result.get("target", {}).get("type") == "inbox" for result in results)
