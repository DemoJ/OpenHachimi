"""定时任务时间计算。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from croniter import croniter
except ImportError:  # pragma: no cover
    croniter = None

from openhachimi_agent.scheduler.models import ScheduleType


def parse_datetime(value: str, tz_name: str = "UTC") -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("时间不能为空。")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(timezone.utc)


def parse_interval_seconds(value: str) -> int:
    text = value.strip().lower()
    if not text:
        raise ValueError("间隔不能为空。")
    units = {
        "s": 1,
        "sec": 1,
        "secs": 1,
        "second": 1,
        "seconds": 1,
        "m": 60,
        "min": 60,
        "mins": 60,
        "minute": 60,
        "minutes": 60,
        "h": 3600,
        "hr": 3600,
        "hour": 3600,
        "hours": 3600,
        "d": 86400,
        "day": 86400,
        "days": 86400,
    }
    for unit, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if text.endswith(unit):
            number = text[: -len(unit)].strip()
            seconds = int(float(number) * multiplier)
            if seconds <= 0:
                raise ValueError("间隔必须大于 0。")
            return seconds
    seconds = int(float(text))
    if seconds <= 0:
        raise ValueError("间隔必须大于 0。")
    return seconds


def compute_next_run(
    schedule_type: ScheduleType | str,
    schedule_expr: str,
    *,
    after: datetime | None = None,
    timezone_name: str = "UTC",
) -> datetime | None:
    kind = ScheduleType(schedule_type)
    base = after or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base = base.astimezone(timezone.utc)

    if kind == ScheduleType.ONCE:
        return parse_datetime(schedule_expr, timezone_name)
    if kind == ScheduleType.INTERVAL:
        return base + timedelta(seconds=parse_interval_seconds(schedule_expr))
    if kind == ScheduleType.CRON:
        if croniter is None:
            raise RuntimeError("请先安装 croniter 以启用 cron 定时任务。")
        local_base = base.astimezone(ZoneInfo(timezone_name))
        return croniter(schedule_expr, local_base).get_next(datetime).astimezone(timezone.utc)
    raise ValueError(f"不支持的定时类型：{schedule_type}")
