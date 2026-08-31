"""Fail-closed event-source gate for the single primer workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from schedule_logic import due_window, validate_plan


@dataclass(frozen=True)
class GateDecision:
    due: bool
    primer: datetime | None
    reason: str


def decide_workflow_gate(plan: dict, now: datetime, *, event_name: str,
                         dispatch_source: str = "",
                         wakeup_schedule: str | None = None) -> GateDecision:
    timing = validate_plan(plan)
    if event_name == "workflow_dispatch":
        if dispatch_source == "manual":
            return GateDecision(True, timing.primer, "manual dispatch bypassed the calendar gate")
        if dispatch_source == "cloudflare":
            due, primer = due_window(plan, now)
            return GateDecision(due, primer, "cloudflare dispatch checked the calendar gate")
        return GateDecision(False, None, "unknown dispatch source rejected")
    if event_name == "schedule":
        due, primer = due_window(plan, now, wakeup_schedule=wakeup_schedule)
        return GateDecision(due, primer, "native schedule checked its exact wake-up cron")
    return GateDecision(False, None, "unsupported event rejected")
