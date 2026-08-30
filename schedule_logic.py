"""Pure schedule calculations shared by the workflow and its tests.

This module has no GitHub, filesystem, or credential behavior. It only turns
the structured local-time plan into a prime time, due decision, or local cron.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


INERT_CRON = "0 0 1 1 *"
TOLERANCE_BEFORE = timedelta(minutes=10)
TOLERANCE_AFTER = timedelta(hours=2)


@dataclass(frozen=True)
class Timing:
    zone: tzinfo
    work_start: datetime
    window_duration: timedelta
    reset_after_start: timedelta
    lead: timedelta
    primer: datetime


def _timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows runners without the system IANA database still need the
        # project's fixed Beijing timezone for local static tests/bootstrap.
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name)
        raise ValueError("unknown timezone")


def _local_datetime(value: str, zone: tzinfo) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def validate_plan(plan: dict) -> Timing:
    mode = plan.get("mode")
    if mode not in {"once", "daily", "weekly"}:
        raise ValueError("unsupported mode")
    zone = _timezone(str(plan["timezone"]))
    work_start = _local_datetime(str(plan["work_start_local"]), zone)
    window_minutes = int(plan.get("window_duration_minutes", 300))
    reset_minutes = int(plan.get("reset_after_start_minutes", 90))
    if window_minutes <= 0 or reset_minutes < 0 or reset_minutes >= window_minutes:
        raise ValueError("invalid window/reset timing")
    active_from = date.fromisoformat(str(plan["active_from_local"]))
    active_until_value = plan.get("active_until_local")
    if active_until_value is not None:
        active_until = date.fromisoformat(str(active_until_value))
        if active_until < active_from:
            raise ValueError("active range is reversed")
    for value in plan.get("skip_dates_local", []):
        date.fromisoformat(str(value))
    window = timedelta(minutes=window_minutes)
    reset = timedelta(minutes=reset_minutes)
    lead = window - reset
    return Timing(zone, work_start, window, reset, lead, work_start - lead)


def _active(plan: dict, work_date: date) -> bool:
    active_from = date.fromisoformat(str(plan["active_from_local"]))
    active_until_value = plan.get("active_until_local")
    active_until = date.fromisoformat(str(active_until_value)) if active_until_value else None
    skipped = {date.fromisoformat(str(value)) for value in plan.get("skip_dates_local", [])}
    return (
        work_date >= active_from
        and (active_until is None or work_date <= active_until)
        and work_date not in skipped
    )


def due_window(plan: dict, now: datetime) -> tuple[bool, datetime]:
    """Return whether now is in the tolerated prime window and its candidate."""

    timing = validate_plan(plan)
    local_now = (
        now.replace(tzinfo=timing.zone)
        if now.tzinfo is None
        else now.astimezone(timing.zone)
    )
    mode = plan["mode"]
    if mode == "once":
        candidate = timing.primer
        return (
            _active(plan, timing.work_start.date())
            and candidate - TOLERANCE_BEFORE <= local_now <= candidate + TOLERANCE_AFTER,
            candidate,
        )

    target_time = timing.work_start.timetz().replace(tzinfo=None)
    for day_delta in range(-2, 3):
        work_date = local_now.date() + timedelta(days=day_delta)
        if mode == "weekly" and work_date.weekday() != timing.work_start.weekday():
            continue
        if not _active(plan, work_date):
            continue
        work_start = datetime.combine(work_date, target_time, tzinfo=timing.zone)
        candidate = work_start - timing.lead
        if candidate - TOLERANCE_BEFORE <= local_now <= candidate + TOLERANCE_AFTER:
            return True, candidate
    return False, timing.primer


def cron_for(plan: dict) -> str:
    if not plan.get("enabled", False):
        return INERT_CRON
    timing = validate_plan(plan)
    local_primer = timing.primer
    minute, hour = local_primer.minute, local_primer.hour
    if plan["mode"] == "once":
        return f"{minute} {hour} {local_primer.day} {local_primer.month} *"
    if plan["mode"] == "daily":
        return f"{minute} {hour} * * *"
    github_weekday = (local_primer.weekday() + 1) % 7
    return f"{minute} {hour} * * {github_weekday}"


def prime_time(plan: dict) -> datetime:
    return validate_plan(plan).primer
