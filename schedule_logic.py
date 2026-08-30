"""Pure multi-slot schedule calculations.

The JSON plan is the business source of truth.  This module contains no
GitHub, filesystem, or credential behavior; it only normalizes v1/v2 plans,
calculates prime times, detects window collisions, generates GitHub cron
wake-ups, and gates due slots.
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
        return _resolve_local_wall_datetime(
            datetime.combine(self.work_date, self.spec.clock),
            self.zone,
        )

    @property
    def primer(self) -> datetime:
        lead = self.window_duration - self.spec.reset_after_start
        return (
            _utc_instant(self.work_start) - lead
        ).astimezone(self.zone)


@dataclass(frozen=True)
class PrimeCollision:
    """Two effective prime requests that cannot start independent windows."""

    earlier: ScheduledSlot
    later: ScheduledSlot
    gap_minutes: float


@dataclass(frozen=True)
class DispatchCollision:
    """A future prime that falls inside a same-day immediate dispatch window."""

    dispatch_time: datetime
    later: ScheduledSlot
    gap_minutes: float


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


def _utc_instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _resolve_local_wall_datetime(value: datetime, zone: tzinfo) -> datetime:
    """Resolve a local wall time, rejecting gaps and choosing the earlier fold."""

    naive = value.replace(tzinfo=None)
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = (
            _utc_instant(candidate)
            .astimezone(zone)
            .replace(tzinfo=None)
        )
        if round_trip == naive:
            candidates.append(candidate)
    if not candidates:
        raise ValueError("nonexistent local work time")
    # An ambiguous wall time has two valid instants. Pick the earlier one
    # explicitly; a caller that needs the later fold must use an explicit
    # offset in the v1 work_start_local value.
    return min(candidates, key=_utc_instant)


def _local_datetime(value: str, zone: tzinfo) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return _resolve_local_wall_datetime(parsed, zone)
    # Validate the supplied local wall clock even when the input includes an
    # offset; this rejects a spring-forward gap instead of silently shifting it.
    _resolve_local_wall_datetime(parsed.replace(tzinfo=None), zone)
    return _utc_instant(parsed).astimezone(zone)


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
        if isinstance(value, SlotSpec):
            clock = value.clock
            reset_minutes = value.reset_after_start_minutes
        elif isinstance(value, str):
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
    if enabled and any(weekly) and active_from is None:
        raise ValueError("enabled recurring v2 plan requires active_from_local")

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


def _edit_slot_specs(
    values: object,
    default_reset: int,
    window_duration: timedelta,
) -> tuple[SlotSpec, ...]:
    if isinstance(values, (str, dict, SlotSpec)):
        values = [values]
    return _parse_slot_specs(values, default_reset, window_duration)


def _slot_json(slot: SlotSpec, default_reset: int) -> str | dict[str, object]:
    clock = slot.clock.strftime("%H:%M")
    if slot.reset_after_start_minutes == default_reset:
        return clock
    return {
        "time": clock,
        "reset_after_start_minutes": slot.reset_after_start_minutes,
    }


def _date_rule_json(
    rule: DateRule,
    default_reset: int,
) -> dict[str, object]:
    result: dict[str, object] = {"mode": rule.mode}
    if rule.mode != "cancel" or rule.slots:
        result["slots"] = [
            _slot_json(slot, default_reset) for slot in rule.slots
        ]
    return result


def _with_date_rule(
    plan: dict,
    work_date: date,
    rule: DateRule | None,
) -> dict:
    updated = dict(plan)
    dates = dict(plan.get("dates", {}))
    key = work_date.isoformat()
    if rule is None:
        dates.pop(key, None)
    else:
        normalized = _normalize(plan)
        dates[key] = _date_rule_json(
            rule,
            normalized.default_reset_after_start_minutes,
        )
    updated["dates"] = dates
    return updated


def apply_date_rule_edit(
    plan: dict,
    work_date: date | str,
    operation: str,
    slots: object | None = None,
) -> dict:
    """Apply one deterministic edit to a v2 date rule.

    The current effective slots for ``work_date`` are the input state. ``add``
    (also ``extra``/``append``) adds to that state, ``cancel`` removes the
    requested clocks (or the whole day when no clocks are supplied), and
    ``replace`` (also ``override``) replaces the target scope. A mixed history
    that cannot be represented by one ``extra`` or ``cancel`` rule is stored as
    a complete ``override`` so earlier edits cannot be lost. The input plan is
    not mutated; the returned plan is safe to pass to the next edit.
    """

    if plan.get("version", 1) != 2:
        raise ValueError("date-rule edits require a v2 plan")
    normalized = _normalize(plan)
    target_date = _parse_date(work_date, "work date") if isinstance(work_date, str) else work_date
    if isinstance(target_date, datetime) or not isinstance(target_date, date):
        raise ValueError("invalid work date")
    if not isinstance(operation, str):
        raise ValueError("invalid date-rule operation")
    operation_name = operation.strip().lower()
    if operation_name in {"add", "extra", "append"}:
        operation_name = "add"
    elif operation_name in {"replace", "override"}:
        operation_name = "replace"
    elif operation_name != "cancel":
        raise ValueError("invalid date-rule operation")

    current = _effective_specs(normalized, target_date)
    current_by_clock = {slot.clock: slot for slot in current}
    base = (
        normalized.weekly[target_date.weekday()]
        if _active(normalized, target_date)
        else ()
    )
    base_clocks = {slot.clock for slot in base}
    existing = normalized.dates.get(target_date)

    if operation_name == "replace":
        if slots is None:
            raise ValueError("replace needs slots")
        replacement = _edit_slot_specs(
            slots,
            normalized.default_reset_after_start_minutes,
            normalized.window_duration,
        )
        if not replacement:
            raise ValueError("replace needs at least one slot")
        return _with_date_rule(
            plan,
            target_date,
            DateRule("override", replacement),
        )

    if operation_name == "add":
        if slots is None:
            raise ValueError("add needs slots")
        additions = _edit_slot_specs(
            slots,
            normalized.default_reset_after_start_minutes,
            normalized.window_duration,
        )
        final_by_clock = dict(current_by_clock)
        for slot in additions:
            # Adding an already-effective clock is idempotent and preserves
            # its established reset timing.
            final_by_clock.setdefault(slot.clock, slot)
        final = tuple(sorted(final_by_clock.values(), key=lambda slot: slot.clock))

        if existing is None:
            extra = tuple(slot for slot in additions if slot.clock not in base_clocks)
            if not extra:
                return dict(plan)
            return _with_date_rule(plan, target_date, DateRule("extra", extra))
        if existing.mode == "extra":
            current_clocks = set(current_by_clock)
            new_extra = tuple(
                slot for slot in additions if slot.clock not in current_clocks
            )
            if not new_extra:
                return dict(plan)
            return _with_date_rule(
                plan,
                target_date,
                DateRule("extra", tuple(existing.slots) + new_extra),
            )
        # A prior cancel and a new add, or a prior override and a new add,
        # need the complete effective result to preserve both intentions.
        return _with_date_rule(plan, target_date, DateRule("override", final))

    # A cancel without clocks means cancel the whole date, regardless of the
    # previous rule mode.
    if slots is None:
        return _with_date_rule(plan, target_date, DateRule("cancel"))
    canceled_specs = _edit_slot_specs(
        slots,
        normalized.default_reset_after_start_minutes,
        normalized.window_duration,
    )
    canceled_clocks = {slot.clock for slot in canceled_specs}
    if not canceled_clocks:
        return _with_date_rule(plan, target_date, DateRule("cancel"))

    final_by_clock = {
        clock: slot
        for clock, slot in current_by_clock.items()
        if clock not in canceled_clocks
    }
    final = tuple(sorted(final_by_clock.values(), key=lambda slot: slot.clock))

    if existing is None:
        base_canceled = tuple(
            sorted(base_clocks & canceled_clocks)
        )
        if not base_canceled:
            return dict(plan)
        return _with_date_rule(
            plan,
            target_date,
            DateRule("cancel", tuple(SlotSpec(clock, normalized.default_reset_after_start_minutes) for clock in base_canceled)),
        )

    if existing.mode == "cancel":
        if not existing.slots:
            return dict(plan)
        already_canceled = {slot.clock for slot in existing.slots}
        merged = tuple(
            SlotSpec(clock, normalized.default_reset_after_start_minutes)
            for clock in sorted(already_canceled | (base_clocks & canceled_clocks))
        )
        if not merged:
            return dict(plan)
        return _with_date_rule(plan, target_date, DateRule("cancel", merged))

    if existing.mode == "extra":
        remaining_extra = tuple(
            slot for slot in existing.slots if slot.clock not in canceled_clocks
        )
        base_canceled = base_clocks & canceled_clocks
        if base_canceled and remaining_extra:
            return _with_date_rule(plan, target_date, DateRule("override", final))
        if remaining_extra:
            return _with_date_rule(
                plan,
                target_date,
                DateRule("extra", remaining_extra),
            )
        if base_canceled:
            return _with_date_rule(
                plan,
                target_date,
                DateRule(
                    "cancel",
                    tuple(
                        SlotSpec(clock, normalized.default_reset_after_start_minutes)
                        for clock in sorted(base_canceled)
                    ),
                ),
            )
        return _with_date_rule(plan, target_date, None)

    # A prior override has no separate base/additive representation. Keep the
    # complete remaining effective slots, using whole-day cancel for empty.
    if final:
        return _with_date_rule(plan, target_date, DateRule("override", final))
    return _with_date_rule(plan, target_date, DateRule("cancel"))


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


def _recurring_projection_dates(normalized: NormalizedPlan) -> tuple[date, date]:
    """Choose a bounded recurring sample that includes possible DST changes."""

    span = timedelta(days=365)
    if normalized.active_from is not None:
        start = normalized.active_from
    elif normalized.active_until is not None:
        start = normalized.active_until - span
    else:
        start = _REFERENCE_MONDAY
    if normalized.active_until is not None:
        start = min(start, normalized.active_until - span)
        end = min(start + span, normalized.active_until)
    else:
        end = start + span
    return start, end


def _cron_layout(normalized: NormalizedPlan, now: datetime | None = None) -> _CronLayout:
    local_now = _local_now(now, normalized.zone) if now is not None else None
    local_now_utc = _utc_instant(local_now) if local_now is not None else None
    recurring_by_time: dict[tuple[int, int], set[int]] = defaultdict(set)
    projection_start, projection_end = _recurring_projection_dates(normalized)
    current = projection_start
    while current <= projection_end:
        if _active(normalized, current):
            for spec in normalized.weekly[current.weekday()]:
                instance = ScheduledSlot(current, spec, normalized.zone, normalized.window_duration)
                primer = instance.primer
                recurring_by_time[(primer.hour, primer.minute)].add(_github_weekday(primer.date()))
        current += timedelta(days=1)

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
            if (
                local_now_utc is not None
                and _utc_instant(primer) < local_now_utc + TOLERANCE_BEFORE
            ):
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
    """Return wake-ups, omitting expired or dispatch-bound dated prime times."""

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
    return _resolve_local_wall_datetime(now, zone) if now.tzinfo is None else now.astimezone(zone)


def _wakeup_for_instance(layout: _CronLayout, instance: ScheduledSlot) -> str | None:
    primer = instance.primer
    recurring = layout.recurring.get((primer.minute, primer.hour, _github_weekday(primer.date())))
    if recurring is not None:
        return recurring
    return layout.dated.get((primer.date(), primer.minute, primer.hour))


def _target_clocks(values: object) -> set[time]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("targeted_slots must be a collection of slot times")
    clocks: set[time] = set()
    for value in values:
        if isinstance(value, SlotSpec):
            clocks.add(value.clock)
        elif isinstance(value, dict):
            if "time" not in value:
                raise ValueError("targeted slot object needs time")
            clocks.add(_parse_clock(value["time"]))
        else:
            clocks.add(_parse_clock(value))
    return clocks


def _target_date(value: date | str | None, fallback: date) -> date:
    if value is None:
        return fallback
    if isinstance(value, datetime):
        raise ValueError("target date must be a date")
    if isinstance(value, date):
        return value
    return _parse_date(value, "target date")


def _targeted_prime_action(
    normalized: NormalizedPlan,
    local_now: datetime,
    target_date: date,
    targeted_slots: object | None,
) -> str:
    if not normalized.enabled:
        return "none"
    date_rule = normalized.dates.get(target_date)
    if date_rule is not None and date_rule.mode == "cancel":
        return "none"

    instances = _instances_for_date(normalized, target_date)
    if targeted_slots is None:
        if date_rule is None:
            return "none"
        target_clocks = {slot.clock for slot in date_rule.slots}
    else:
        target_clocks = _target_clocks(targeted_slots)
    instances = tuple(instance for instance in instances if instance.spec.clock in target_clocks)
    if not instances:
        return "none"

    now_instant = _utc_instant(local_now)
    if any(
        _utc_instant(instance.primer) - now_instant < TOLERANCE_BEFORE
        for instance in instances
    ):
        return "dispatch"
    return "schedule"


def targeted_prime_action(
    plan: dict,
    now: datetime,
    target_date: date | str | None = None,
    targeted_slots: object | None = None,
) -> str:
    """Classify explicitly targeted dated work as schedule, dispatch, or none.

    ``target_date`` is the work date, not necessarily the local calendar date
    of its prime. This distinction matters when a dated work slot crosses
    midnight, for example a 02:00 work start whose prime is on the previous
    evening. With no explicit ``targeted_slots``, only that date rule's slots
    are considered; unrelated recurring slots cannot hide a missed target.
    If several targeted slots are present, one missed or near-term prime still
    returns ``dispatch`` while future targets remain on the normal cron path.
    Prime comparisons use UTC instants. This helper never dispatches anything
    itself.
    """

    normalized = _normalize(plan)
    if not normalized.enabled:
        return "none"
    local_now = _local_now(now, normalized.zone)
    resolved_date = _target_date(target_date, local_now.date())
    return _targeted_prime_action(
        normalized,
        local_now,
        resolved_date,
        targeted_slots,
    )


def today_prime_action(
    plan: dict,
    now: datetime,
    targeted_slots: object | None = None,
) -> str:
    """Backward-compatible wrapper for a target whose work date is today.

    Call :func:`targeted_prime_action` for a dated rule on another work date.
    """

    normalized = _normalize(plan)
    local_now = _local_now(now, normalized.zone)
    return _targeted_prime_action(
        normalized,
        local_now,
        local_now.date(),
        targeted_slots,
    )


def _collision_date_intervals(normalized: NormalizedPlan) -> tuple[tuple[date, date], ...]:
    """Return bounded calendar windows sufficient to inspect recurring and dated slots."""

    padding_days = max(2, ceil(normalized.window_duration.total_seconds() / 86400) + 1)
    padding = timedelta(days=padding_days)
    intervals: list[tuple[date, date]] = []

    if any(normalized.weekly):
        # Use the same stable projection as cron generation, plus neighboring
        # days for prime times that cross midnight.
        projection_start, projection_end = _recurring_projection_dates(normalized)
        intervals.append((projection_start - padding, projection_end + padding))

    for work_date in normalized.dates:
        intervals.append((work_date - padding, work_date + padding))

    if not intervals:
        return ()

    merged: list[list[date]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def find_collisions(plan: dict) -> tuple[PrimeCollision, ...]:
    """Find adjacent effective prime requests closer than one usage window.

    Recurring schedules are inspected across a calendar year, while every dated
    rule is inspected with enough neighboring calendar days to catch
    cross-midnight and cross-week collisions. Prime instants are ordered on
    the UTC timeline, so DST transitions use elapsed time rather than a
    naive wall-clock subtraction. This reports effective occurrences only;
    a dated override can therefore remove a collision inside its active
    range without changing unrelated recurring occurrences.
    """

    normalized = _normalize(plan)
    if not normalized.enabled:
        return ()
    _require_slots(normalized)

    work_dates: set[date] = set()
    for start, end in _collision_date_intervals(normalized):
        current = start
        while current <= end:
            work_dates.add(current)
            current += timedelta(days=1)

    instances: dict[tuple[date, time], ScheduledSlot] = {}
    for work_date in sorted(work_dates):
        for instance in _instances_for_date(normalized, work_date):
            instances[(instance.work_date, instance.spec.clock)] = instance

    ordered = sorted(
        instances.values(),
        key=lambda instance: _utc_instant(instance.primer),
    )
    prime_groups: list[tuple[datetime, list[ScheduledSlot]]] = []
    for instance in ordered:
        instant = _utc_instant(instance.primer)
        if prime_groups and prime_groups[-1][0] == instant:
            prime_groups[-1][1].append(instance)
        else:
            prime_groups.append((instant, [instance]))

    window_seconds = normalized.window_duration.total_seconds()
    collisions: list[PrimeCollision] = []
    seen_signatures: set[tuple[object, ...]] = set()
    for (earlier_instant, earlier_group), (later_instant, later_group) in zip(
        prime_groups,
        prime_groups[1:],
    ):
        earlier = earlier_group[0]
        later = later_group[0]
        gap_seconds = (
            later_instant
            - earlier_instant
        ).total_seconds()
        if gap_seconds < window_seconds:
            if earlier.work_date in normalized.dates or later.work_date in normalized.dates:
                signature: tuple[object, ...] = (
                    "dated",
                    earlier.work_date,
                    earlier.spec.clock,
                    earlier.spec.reset_after_start_minutes,
                    later.work_date,
                    later.spec.clock,
                    later.spec.reset_after_start_minutes,
                )
            else:
                signature = (
                    "recurring",
                    earlier.work_date.weekday(),
                    earlier.spec.clock,
                    earlier.spec.reset_after_start_minutes,
                    later.work_date.weekday(),
                    later.spec.clock,
                    later.spec.reset_after_start_minutes,
                )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            collisions.append(PrimeCollision(earlier, later, gap_seconds / 60))
    return tuple(collisions)


def find_dispatch_collisions(
    plan: dict,
    dispatch_time: datetime,
) -> tuple[DispatchCollision, ...]:
    """Find future primes that an immediate best-effort dispatch would cover.

    ``dispatch_time`` is a temporary prime only for this decision; it is not
    persisted in the schedule. Future effective prime instants strictly less
    than one window after it are returned. Exact duplicate future instants are
    grouped because they share one window.
    """

    normalized = _normalize(plan)
    if not normalized.enabled:
        return ()
    _require_slots(normalized)

    local_dispatch = _local_now(dispatch_time, normalized.zone)
    dispatch_instant = _utc_instant(local_dispatch)
    window_seconds = normalized.window_duration.total_seconds()
    padding_days = max(2, ceil((2 * window_seconds) / 86400) + 1)
    start = local_dispatch.date() - timedelta(days=padding_days)
    end = local_dispatch.date() + timedelta(days=padding_days)
    future: dict[datetime, ScheduledSlot] = {}
    current = start
    while current <= end:
        for instance in _instances_for_date(normalized, current):
            instant = _utc_instant(instance.primer)
            gap_seconds = (instant - dispatch_instant).total_seconds()
            if 0 < gap_seconds < window_seconds:
                future.setdefault(instant, instance)
        current += timedelta(days=1)

    return tuple(
        DispatchCollision(
            local_dispatch,
            instance,
            (instant - dispatch_instant).total_seconds() / 60,
        )
        for instant, instance in sorted(future.items())
    )


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
    local_now_utc = _utc_instant(local_now)
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
            candidate_utc = _utc_instant(candidate)
            if not (
                candidate_utc - TOLERANCE_BEFORE
                <= local_now_utc
                <= candidate_utc + TOLERANCE_AFTER
            ):
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
