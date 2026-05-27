"""定时任务数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal


class ScheduleType(str, Enum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


TaskStatus = Literal["enabled", "paused", "deleted"]
RunStatus = Literal["running", "succeeded", "failed", "skipped", "timeout"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ScheduledTask:
    id: str
    name: str
    prompt: str
    schedule_type: ScheduleType
    schedule_expr: str
    role: str | None = None
    session_id: str | None = None
    timezone: str = "UTC"
    enabled: bool = True
    next_run_at: datetime | None = None
    timeout_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    running: bool = False
    locked_until: datetime | None = None


@dataclass(frozen=True)
class ScheduledRun:
    id: str
    task_id: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    output: str | None = None
    error: str | None = None
    duration_ms: int | None = None
