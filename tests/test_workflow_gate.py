import unittest
from datetime import datetime, timedelta, timezone

from workflow_gate import decide_workflow_gate

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def plan():
    return {
        "version": 2, "enabled": True, "timezone": "Asia/Shanghai",
        "window_duration_minutes": 300, "reset_after_start_minutes": 90,
        "active_from_local": "2030-01-01", "active_until_local": None,
        "weekly": {str(day): ["09:00", "20:00"] for day in range(7)}, "dates": {},
    }


class WorkflowGateTests(unittest.TestCase):
    def test_manual_dispatch_bypasses_calendar_gate(self):
        decision = decide_workflow_gate(plan(), datetime(2030, 1, 2, 12, 0, tzinfo=BEIJING), event_name="workflow_dispatch", dispatch_source="manual")
        self.assertTrue(decision.due)

    def test_cloudflare_dispatch_runs_when_current_slot_is_due(self):
        decision = decide_workflow_gate(plan(), datetime(2030, 1, 2, 5, 30, tzinfo=BEIJING), event_name="workflow_dispatch", dispatch_source="cloudflare")
        self.assertTrue(decision.due)

    def test_cloudflare_dispatch_skips_outside_due_window(self):
        decision = decide_workflow_gate(plan(), datetime(2030, 1, 2, 12, 0, tzinfo=BEIJING), event_name="workflow_dispatch", dispatch_source="cloudflare")
        self.assertFalse(decision.due)

    def test_unknown_dispatch_source_is_rejected(self):
        decision = decide_workflow_gate(plan(), datetime(2030, 1, 2, 5, 30, tzinfo=BEIJING), event_name="workflow_dispatch", dispatch_source="unexpected")
        self.assertFalse(decision.due)

    def test_native_schedule_retains_exact_wakeup_gate(self):
        now = datetime(2030, 1, 2, 5, 30, tzinfo=BEIJING)
        matching = decide_workflow_gate(plan(), now, event_name="schedule", wakeup_schedule="30 5 * * *")
        wrong = decide_workflow_gate(plan(), now, event_name="schedule", wakeup_schedule="30 16 * * *")
        self.assertTrue(matching.due)
        self.assertFalse(wrong.due)


if __name__ == "__main__":
    unittest.main()
