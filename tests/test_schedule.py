import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schedule_logic import cron_for, due_window, prime_time, validate_plan


BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def plan(**overrides):
    value = {
        "version": 1,
        "enabled": True,
        "mode": "once",
        "timezone": "Asia/Shanghai",
        "window_duration_minutes": 300,
        "reset_after_start_minutes": 90,
        "work_start_local": "2030-01-02T10:00+08:00",
        "active_from_local": "2030-01-02",
        "active_until_local": "2030-01-02",
        "skip_dates_local": [],
    }
    value.update(overrides)
    return value


class ScheduleTests(unittest.TestCase):
    def test_once(self):
        value = plan()
        self.assertEqual(prime_time(value).isoformat(), "2030-01-02T06:30:00+08:00")
        due, _ = due_window(value, datetime(2030, 1, 2, 6, 30, tzinfo=BEIJING))
        self.assertTrue(due)

    def test_daily(self):
        value = plan(
            mode="daily",
            active_until_local=None,
        )
        due, _ = due_window(value, datetime(2030, 1, 3, 6, 30, tzinfo=BEIJING))
        self.assertTrue(due)
        self.assertEqual(cron_for(value), "30 22 * * *")

    def test_weekly(self):
        value = plan(
            mode="weekly",
            work_start_local="2030-01-07T10:00+08:00",  # Monday
            active_from_local="2030-01-07",
            active_until_local=None,
        )
        due, _ = due_window(value, datetime(2030, 1, 7, 6, 30, tzinfo=BEIJING))
        self.assertTrue(due)
        self.assertEqual(cron_for(value), "30 22 * * 0")

    def test_prime_crosses_midnight(self):
        value = plan(
            work_start_local="2030-01-02T02:00+08:00",
            reset_after_start_minutes=120,
            active_from_local="2030-01-02",
            active_until_local="2030-01-02",
        )
        self.assertEqual(prime_time(value).isoformat(), "2030-01-01T23:00:00+08:00")
        due, _ = due_window(value, datetime(2030, 1, 1, 23, 0, tzinfo=BEIJING))
        self.assertTrue(due)
        self.assertEqual(cron_for(value), "0 15 1 1 *")

    def test_skip_dates(self):
        value = plan(
            mode="daily",
            active_until_local=None,
            skip_dates_local=["2030-01-03"],
        )
        due, _ = due_window(value, datetime(2030, 1, 3, 6, 30, tzinfo=BEIJING))
        self.assertFalse(due)

    def test_active_range(self):
        value = plan(
            mode="daily",
            active_from_local="2030-01-04",
            active_until_local="2030-01-05",
        )
        before, _ = due_window(value, datetime(2030, 1, 3, 6, 30, tzinfo=BEIJING))
        inside, _ = due_window(value, datetime(2030, 1, 4, 6, 30, tzinfo=BEIJING))
        after, _ = due_window(value, datetime(2030, 1, 6, 6, 30, tzinfo=BEIJING))
        self.assertFalse(before)
        self.assertTrue(inside)
        self.assertFalse(after)

    def test_invalid_reset_after_start(self):
        with self.assertRaises(ValueError):
            validate_plan(plan(reset_after_start_minutes=300))

    def test_schedule_and_workflow_cron_match(self):
        root = Path(__file__).resolve().parents[1]
        schedule = json.loads((root / "schedule.json").read_text(encoding="utf-8"))
        workflow = (root / ".github" / "workflows" / "codex-window-primer.yml").read_text(encoding="utf-8")
        match = re.search(r'^\s*- cron: "([^"]+)"', workflow, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), cron_for(schedule))


if __name__ == "__main__":
    unittest.main()
