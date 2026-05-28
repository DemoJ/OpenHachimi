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
DeliveryStatus = Literal[
    "pending",
    "not_required",
    "resolved_empty",
    "delivered",
    "partial",
    "failed",
    "fallback_delivered",
    "fallback_failed",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ScheduledTask:
    id: str
    name: str
    prompt: str
    schedule_type: ScheduleType
    schedule_expr: str
    timezone: str = "UTC"
    status: str = "enabled"
    role: str | None = None
    session_id: str | None = None
    timeout_seconds: int | None = None
    origin: dict[str, Any] = field(default_factory=dict)
    delivery_mode: str = "origin"
    delivery_targets: list[dict[str, Any]] = field(default_factory=list)
    delivery_fallback: dict[str, Any] = field(default_factory=dict)
    execution_policy: dict[str, Any] = field(default_factory=dict)
    safety_status: str | None = None
    safety_error: str | None = None
    next_run_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_delivery_status: str | None = None
    last_delivery_error: str | None = None
    running: bool = False
    locked_until: datetime | None = None

    @property
    def enabled(self) -> bool:
        return self.status == "enabled"


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
    delivery_status: str | None = None
    delivery_targets: list[dict[str, Any]] = field(default_factory=list)
    delivery_results: list[dict[str, Any]] = field(default_factory=list)
    delivery_error: str | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None
    safety_status: str | None = None
    safety_error: str | None = None
    execution_context: dict[str, Any] = field(default_factory=dict)
