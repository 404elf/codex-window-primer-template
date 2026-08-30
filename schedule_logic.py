"""Pure multi-slot schedule calculations.

The JSON plan is the business source of truth.  This module contains no
GitHub, filesystem, or credential behavior; it only normalizes v1/v2 plans,
calculates prime times, generates GitHub cron wake-ups, and gates due slots.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from math import ceil
from re import fullmatch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


INERT_CRON = "0 0 1 1 *"
TOLERANCE_BEFORE = timedelta(minutes=10)
TOLERANCE_AFTER = timedelta(hours=2)
_CLOCK_PATTERN = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
_REFERENCE_MONDAY = date(2000, 1, 3)


@dataclass(frozen=True)
class SlotSpec:
    clock: time
    reset_after_start_minutes: int

    @property
    def reset_after_start(self) -> timedelta:
        return timedelta(minutes=self.reset_after_start_minutes)


@dataclass(frozen=True)
class DateRule:
    mode: str
    slots: tuple[SlotSpec, ...] = ()


@dataclass(frozen=True)
class NormalizedPlan:
    enabled: bool
    zone: tzinfo
    window_duration: timedelta
    default_reset_after_start_minutes: int
    active_from: date | None
    active_until: date | None
    weekly: tuple[tuple[SlotSpec, ...], ...]
    dates: dict[date, DateRule]


@dataclass(frozen=True)
class ScheduledSlot:
    work_date: date
    spec: SlotSpec
    zone: tzinfo
    window_duration: timedelta

    @property
    def work_start(self) -> datetime:
        return datetime.combine(self.work_date, self.spec.clock, tzinfo=self.zone)

    @property
    def primer(self) -> datetime:
        return self.work_start - (self.window_duration - self.spec.reset_after_start)


@dataclass(frozen=True)
class Timing:
    """Compatibility view retained for callers of the v1 API."""

    zone: tzinfo
    work_start: datetime | None
    window_duration: timedelta
    reset_after_start: timedelta
    lead: timedelta
    primer: datetime | None
    slots: tuple[ScheduledSlot, ...] = ()


@dataclass(frozen=True)
class _CronLayout:
    entries: tuple[str, ...]
    recurring: dict[tuple[int, int, int], str]
    dated: dict[tuple[date, int, int], str]


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


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid {label}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc
    if str(value).strip() != str(result):
        raise ValueError(f"invalid {label}")
    return result


def _parse_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc


def _parse_clock(value: object) -> time:
    if not isinstance(value, str) or fullmatch(_CLOCK_PATTERN, value) is None:
        raise ValueError("invalid slot time")
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _parse_common(plan: dict) -> tuple[bool, tzinfo, timedelta, int, date | None, date | None]:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    enabled = plan.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("invalid enabled")
    timezone_name = plan.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("invalid timezone")
    zone = _timezone(timezone_name)
    window_minutes = _integer(plan.get("window_duration_minutes", 300), "window duration")
    reset_minutes = _integer(plan.get("reset_after_start_minutes", 90), "reset timing")
    if window_minutes <= 0 or reset_minutes < 0 or reset_minutes >= window_minutes:
        raise ValueError("invalid window/reset timing")
    active_from_value = plan.get("active_from_local")
    active_until_value = plan.get("active_until_local")
    active_from = None if active_from_value is None else _parse_date(active_from_value, "active_from")
    active_until = None if active_until_value is None else _parse_date(active_until_value, "active_until")
    if active_from is not None and active_until is not None and active_until < active_from:
        raise ValueError("active range is reversed")
    return enabled, zone, timedelta(minutes=window_minutes), reset_minutes, active_from, active_until


def _parse_slot_specs(values: object, default_reset: int, window_duration: timedelta) -> tuple[SlotSpec, ...]:
    if not isinstance(values, list):
        raise ValueError("slots must be a list")
    result: list[SlotSpec] = []
    seen: set[time] = set()
    for value in values:
        if isinstance(value, str):
            clock = _parse_clock(value)
            reset_minutes = default_reset
        elif isinstance(value, dict):
            if "time" not in value:
                raise ValueError("slot object needs time")
            clock = _parse_clock(value["time"])
            reset_minutes = _integer(value.get("reset_after_start_minutes", default_reset), "slot reset timing")
        else:
            raise ValueError("invalid slot")
        if reset_minutes < 0 or reset_minutes >= int(window_duration.total_seconds() // 60):
            raise ValueError("invalid window/reset timing")
        if clock in seen:
            raise ValueError("duplicate slot time")
        seen.add(clock)
        result.append(SlotSpec(clock, reset_minutes))
    return tuple(sorted(result, key=lambda slot: slot.clock))


def _normalize_v1(plan: dict) -> NormalizedPlan:
    mode = plan.get("mode")
    if mode not in {"once", "daily", "weekly"}:
        raise ValueError("unsupported mode")
    enabled, zone, window_duration, default_reset, active_from, active_until = _parse_common(plan)
    if active_from is None:
        raise ValueError("v1 active_from_local is required")
    work_start_value = plan.get("work_start_local")
    if not isinstance(work_start_value, str):
        raise ValueError("v1 work_start_local is required")
    work_start = _local_datetime(work_start_value, zone)
    if work_start.second or work_start.microsecond:
        raise ValueError("work_start_local must have zero seconds")
    slot = SlotSpec(work_start.timetz().replace(tzinfo=None), default_reset)
    weekly: list[list[SlotSpec]] = [[] for _ in range(7)]
    dates: dict[date, DateRule] = {}
    if mode == "daily":
        weekly = [[slot] for _ in range(7)]
    elif mode == "weekly":
        weekly[work_start.weekday()] = [slot]
    else:
        dates[work_start.date()] = DateRule("override", (slot,))
    for value in plan.get("skip_dates_local", []):
        skipped = _parse_date(value, "skip date")
        dates[skipped] = DateRule("cancel")
    return NormalizedPlan(
        enabled=enabled,
        zone=zone,
        window_duration=window_duration,
        default_reset_after_start_minutes=default_reset,
        active_from=active_from,
        active_until=active_until,
        weekly=tuple(tuple(slots) for slots in weekly),
        dates=dates,
    )


def _normalize_v2(plan: dict) -> NormalizedPlan:
    enabled, zone, window_duration, default_reset, active_from, active_until = _parse_common(plan)
    weekly_value = plan.get("weekly", {})
    if not isinstance(weekly_value, dict):
        raise ValueError("weekly must be an object")
    weekly: list[tuple[SlotSpec, ...]] = [()] * 7
    for key, values in weekly_value.items():
        if str(key) not in {str(index) for index in range(7)}:
            raise ValueError("weekly day must be 0 through 6")
        weekday = _integer(key, "weekly day")
        weekly[weekday] = _parse_slot_specs(values, default_reset, window_duration)

    dates_value = plan.get("dates", {})
    if not isinstance(dates_value, dict):
        raise ValueError("dates must be an object")
    dates: dict[date, DateRule] = {}
    for key, raw_rule in dates_value.items():
        work_date = _parse_date(key, "date rule")
        if not isinstance(raw_rule, dict):
            raise ValueError("date rule must be an object")
        mode = raw_rule.get("mode")
        if mode not in {"override", "extra", "cancel"}:
            raise ValueError("unsupported date rule")
        if mode == "cancel" and "slots" not in raw_rule:
            slots = ()
        else:
            slots = _parse_slot_specs(raw_rule.get("slots", []), default_reset, window_duration)
        if mode in {"override", "extra"} and not slots:
            raise ValueError("override/extra needs at least one slot")
        dates[work_date] = DateRule(mode, slots)
    return NormalizedPlan(
        enabled=enabled,
        zone=zone,
        window_duration=window_duration,
        default_reset_after_start_minutes=default_reset,
        active_from=active_from,
        active_until=active_until,
        weekly=tuple(weekly),
        dates=dates,
    )


def _normalize(plan: dict) -> NormalizedPlan:
    version = plan.get("version", 1)
    if version == 1:
        return _normalize_v1(plan)
    if version == 2:
        return _normalize_v2(plan)
    raise ValueError("unsupported plan version")


def _active(normalized: NormalizedPlan, work_date: date) -> bool:
    return (
        (normalized.active_from is None or work_date >= normalized.active_from)
        and (normalized.active_until is None or work_date <= normalized.active_until)
    )


def _effective_specs(
    normalized: NormalizedPlan,
    work_date: date,
    *,
    respect_active_range: bool = True,
) -> tuple[SlotSpec, ...]:
    if respect_active_range and not _active(normalized, work_date):
        return ()
    specs = list(normalized.weekly[work_date.weekday()])
    rule = normalized.dates.get(work_date)
    if rule is not None:
        if rule.mode == "override":
            specs = list(rule.slots)
        elif rule.mode == "extra":
            specs.extend(rule.slots)
        elif rule.mode == "cancel":
            if not rule.slots:
                return ()
            canceled = {slot.clock for slot in rule.slots}
            specs = [slot for slot in specs if slot.clock not in canceled]
    by_clock = {slot.clock: slot for slot in specs}
    return tuple(sorted(by_clock.values(), key=lambda slot: slot.clock))


def _instances_for_date(
    normalized: NormalizedPlan,
    work_date: date,
    *,
    respect_active_range: bool = True,
) -> tuple[ScheduledSlot, ...]:
    return tuple(
        ScheduledSlot(work_date, spec, normalized.zone, normalized.window_duration)
        for spec in _effective_specs(
            normalized,
            work_date,
            respect_active_range=respect_active_range,
        )
    )


def _reference_instances(normalized: NormalizedPlan) -> tuple[ScheduledSlot, ...]:
    """Return deterministic representative slots for compatibility and cron layout."""

    dates_to_check: set[date] = set()
    for weekday, specs in enumerate(normalized.weekly):
        if specs:
            reference_date = _REFERENCE_MONDAY + timedelta(days=weekday)
            if normalized.active_from is not None:
                days_until_weekday = (weekday - normalized.active_from.weekday()) % 7
                candidate = normalized.active_from + timedelta(days=days_until_weekday)
                if normalized.active_until is None or candidate <= normalized.active_until:
                    reference_date = candidate
            dates_to_check.add(reference_date)
    dates_to_check.update(normalized.dates)
    instances: list[ScheduledSlot] = []
    for work_date in sorted(dates_to_check):
        instances.extend(
            _instances_for_date(normalized, work_date, respect_active_range=False)
        )
    return tuple(sorted(instances, key=lambda item: (item.work_date, item.spec.clock)))


def _require_slots(normalized: NormalizedPlan) -> None:
    configured = any(normalized.weekly) or any(
        rule.mode in {"override", "extra"} and bool(rule.slots)
        for rule in normalized.dates.values()
    )
    if normalized.enabled and not configured:
        raise ValueError("enabled plan has no active slots")


def validate_plan(plan: dict) -> Timing:
    normalized = _normalize(plan)
    _require_slots(normalized)
    slots = _reference_instances(normalized)
    first = slots[0] if slots else None
    window = normalized.window_duration
    reset = first.spec.reset_after_start if first else timedelta(minutes=normalized.default_reset_after_start_minutes)
    lead = window - reset
    return Timing(
        normalized.zone,
        first.work_start if first else None,
        window,
        reset,
        lead,
        first.primer if first else None,
        slots,
    )


def _github_weekday(work_date: date) -> int:
    return (work_date.weekday() + 1) % 7


def _cron_layout(normalized: NormalizedPlan, now: datetime | None = None) -> _CronLayout:
    local_now = _local_now(now, normalized.zone) if now is not None else None
    recurring_by_time: dict[tuple[int, int], set[int]] = defaultdict(set)
    for weekday, specs in enumerate(normalized.weekly):
        reference_date = _REFERENCE_MONDAY + timedelta(days=weekday)
        for spec in specs:
            instance = ScheduledSlot(reference_date, spec, normalized.zone, normalized.window_duration)
            primer = instance.primer
            recurring_by_time[(primer.hour, primer.minute)].add(_github_weekday(primer.date()))

    recurring: dict[tuple[int, int, int], str] = {}
    entries: list[str] = []
    for (hour, minute), weekdays in sorted(recurring_by_time.items()):
        day_field = "*" if len(weekdays) == 7 else ",".join(str(day) for day in sorted(weekdays))
        entry = f"{minute} {hour} * * {day_field}"
        entries.append(entry)
        for weekday in weekdays:
            recurring[(minute, hour, weekday)] = entry

    dated_by_key: dict[tuple[int, int, int], set[int]] = defaultdict(set)
    dated_lookup: list[tuple[date, int, int]] = []
    for work_date in sorted(normalized.dates):
        for instance in _instances_for_date(normalized, work_date):
            primer = instance.primer
            if local_now is not None and primer < local_now:
                continue
            key = (primer.date(), primer.minute, primer.hour)
            if (primer.minute, primer.hour, _github_weekday(primer.date())) in recurring:
                continue
            dated_by_key[(primer.minute, primer.hour, primer.month)].add(primer.day)
            dated_lookup.append(key)
    dated: dict[tuple[date, int, int], str] = {}
    for (minute, hour, month), days in sorted(dated_by_key.items()):
        day_field = ",".join(str(day) for day in sorted(days))
        entry = f"{minute} {hour} {day_field} {month} *"
        entries.append(entry)
        for prime_date, prime_minute, prime_hour in dated_lookup:
            if (prime_minute, prime_hour, prime_date.month) == (minute, hour, month):
                dated[(prime_date, prime_minute, prime_hour)] = entry

    unique_entries = tuple(dict.fromkeys(entries))
    return _CronLayout(unique_entries, recurring, dated)


def cron_entries(plan: dict, now: datetime | None = None) -> tuple[str, ...]:
    """Return the minimal wake-up projection, omitting already-passed dates."""

    normalized = _normalize(plan)
    if not normalized.enabled:
        return (INERT_CRON,)
    _require_slots(normalized)
    layout = _cron_layout(normalized, now=now)
    return layout.entries or (INERT_CRON,)


def cron_for(plan: dict, now: datetime | None = None) -> str:
    entries = cron_entries(plan, now=now)
    if len(entries) != 1:
        raise ValueError("plan has multiple cron entries; use cron_entries")
    return entries[0]


def _local_now(now: datetime, zone: tzinfo) -> datetime:
    return now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)


def _wakeup_for_instance(layout: _CronLayout, instance: ScheduledSlot) -> str | None:
    primer = instance.primer
    recurring = layout.recurring.get((primer.minute, primer.hour, _github_weekday(primer.date())))
    if recurring is not None:
        return recurring
    return layout.dated.get((primer.date(), primer.minute, primer.hour))


def today_prime_action(plan: dict, now: datetime) -> str:
    """Classify an explicit today's date rule as schedule, dispatch, or none.

    This is a pure decision helper for the natural-language controller. It
    never dispatches anything itself. A dated rule whose prime is still ahead
    uses the normal cron path; once all of its effective primes have passed,
    the caller may use the existing workflow_dispatch path if the user's
    wording clearly says that today's work should still happen.
    """

    normalized = _normalize(plan)
    if not normalized.enabled:
        return "none"
    local_now = _local_now(now, normalized.zone)
    if local_now.date() not in normalized.dates:
        return "none"
    instances = _instances_for_date(normalized, local_now.date())
    if not instances:
        return "none"
    if any(instance.primer >= local_now for instance in instances):
        return "schedule"
    return "dispatch"


def due_slots(
    plan: dict,
    now: datetime,
    wakeup_schedule: str | None = None,
) -> tuple[tuple[ScheduledSlot, datetime], ...]:
    """Return due slots and candidates, optionally gated by event cron text."""

    normalized = _normalize(plan)
    if not normalized.enabled:
        return ()
    _require_slots(normalized)
    local_now = _local_now(now, normalized.zone)
    layout = _cron_layout(normalized) if wakeup_schedule else None
    span = max(2, ceil((normalized.window_duration + TOLERANCE_AFTER).total_seconds() / 86400) + 1)
    results: list[tuple[ScheduledSlot, datetime]] = []
    seen: set[tuple[date, time]] = set()
    for day_delta in range(-span, span + 1):
        work_date = local_now.date() + timedelta(days=day_delta)
        for instance in _instances_for_date(normalized, work_date):
            key = (instance.work_date, instance.spec.clock)
            if key in seen:
                continue
            candidate = instance.primer
            if not candidate - TOLERANCE_BEFORE <= local_now <= candidate + TOLERANCE_AFTER:
                continue
            if layout is not None and _wakeup_for_instance(layout, instance) != wakeup_schedule:
                continue
            seen.add(key)
            results.append((instance, candidate))
    return tuple(sorted(results, key=lambda item: (item[1], item[0].work_date, item[0].spec.clock)))


def due_window(
    plan: dict,
    now: datetime,
    wakeup_schedule: str | None = None,
) -> tuple[bool, datetime | None]:
    """Return whether at least one slot is due and its earliest candidate."""

    normalized = _normalize(plan)
    if not normalized.enabled:
        return False, None
    due = due_slots(plan, now, wakeup_schedule=wakeup_schedule)
    if due:
        return True, due[0][1]
    references = _reference_instances(normalized)
    return False, references[0].primer if references else None


def prime_time(plan: dict) -> datetime:
    timing = validate_plan(plan)
    if timing.primer is None:
        raise ValueError("plan has no active slots")
    return timing.primer
