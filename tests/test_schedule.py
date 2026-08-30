import json
import re
import unittest
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest.mock import patch

from schedule_logic import cron_for, due_window, prime_time, validate_plan


BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


class NewYorkTestZone(tzinfo):
    """Small deterministic DST fixture for Windows, which may lack tzdata."""

    def _is_dst(self, value):
        if value is None:
            return False
        local_date = value.replace(tzinfo=None).date()
        return date(2030, 3, 10) <= local_date < date(2030, 11, 3)

    def utcoffset(self, value):
        return timedelta(hours=-4 if self._is_dst(value) else -5)

    def dst(self, value):
        return timedelta(hours=1) if self._is_dst(value) else timedelta(0)

    def tzname(self, value):
        return "EDT" if self._is_dst(value) else "EST"


NEW_YORK = NewYorkTestZone()


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
        self.assertEqual(cron_for(value), "30 6 * * *")

    def test_weekly(self):
        value = plan(
            mode="weekly",
            work_start_local="2030-01-07T10:00+08:00",  # Monday
            active_from_local="2030-01-07",
            active_until_local=None,
        )
        due, _ = due_window(value, datetime(2030, 1, 7, 6, 30, tzinfo=BEIJING))
        self.assertTrue(due)
        self.assertEqual(cron_for(value), "30 6 * * 1")

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
        self.assertEqual(cron_for(value), "0 23 1 1 *")

    def test_new_york_dst_keeps_local_prime_time(self):
        cases = [
            (
                plan(
                    mode="daily",
                    timezone="America/New_York",
                    work_start_local="2030-03-08T09:00:00-05:00",
                    active_from_local="2030-03-08",
                    active_until_local=None,
                ),
                datetime(2030, 3, 8, 5, 30, tzinfo=NEW_YORK),
                -5,
            ),
            (
                plan(
                    mode="daily",
                    timezone="America/New_York",
                    work_start_local="2030-03-11T09:00:00-04:00",
                    active_from_local="2030-03-11",
                    active_until_local=None,
                ),
                datetime(2030, 3, 11, 5, 30, tzinfo=NEW_YORK),
                -4,
            ),
        ]
        for value, now, offset_hours in cases:
            with patch("schedule_logic._timezone", return_value=NEW_YORK):
                timing = validate_plan(value)
                self.assertEqual((timing.primer.hour, timing.primer.minute), (5, 30))
                self.assertEqual(timing.primer.utcoffset(), timedelta(hours=offset_hours))
                due, _ = due_window(value, now)
                self.assertTrue(due)
                self.assertEqual(cron_for(value), "30 5 * * *")

    def test_weekly_midnight_prime_with_timezone(self):
        value = plan(
            mode="weekly",
            timezone="America/New_York",
            work_start_local="2030-01-07T02:00:00-05:00",  # Monday
            reset_after_start_minutes=120,
            active_from_local="2030-01-07",
            active_until_local=None,
        )
        with patch("schedule_logic._timezone", return_value=NEW_YORK):
            timing = validate_plan(value)
            self.assertEqual(timing.primer.date(), date(2030, 1, 6))
            self.assertEqual((timing.primer.hour, timing.primer.minute), (23, 0))
            due, _ = due_window(value, datetime(2030, 1, 6, 23, 0, tzinfo=NEW_YORK))
            self.assertTrue(due)
            self.assertEqual(cron_for(value), "0 23 * * 0")

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
        match = re.search(
            r'^\s*- cron: "([^"]+)"\s*\n\s+timezone: "([^"]+)"',
            workflow,
            re.MULTILINE,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), cron_for(schedule))
        self.assertEqual(match.group(2), schedule["timezone"])

    def test_recovery_path_handles_ciphertext_already_in_head(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "codex-window-primer.yml").read_text(encoding="utf-8")
        function = workflow[workflow.index("preserve_conflict()"):]
        self.assertIn('target_blob="$(git hash-object "$RUNNER_TEMP/auth.json.enc.next")"', function)
        self.assertIn('head_blob="$(git rev-parse "HEAD:auth.json.enc" 2>/dev/null || true)"', function)
        self.assertIn('if [[ "$head_blob" != "$target_blob" ]]; then', function)
        self.assertLess(
            function.index('if [[ "$head_blob" != "$target_blob" ]]; then'),
            function.index('git commit -m "chore: preserve encrypted Codex auth recovery state"'),
        )
        self.assertLess(
            function.index('git commit -m "chore: preserve encrypted Codex auth recovery state"'),
            function.index('git push origin "HEAD:refs/heads/$recovery_branch"'),
        )

    def test_failed_codex_still_reaches_state_and_final_failure(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "codex-window-primer.yml").read_text(encoding="utf-8")
        self.assertIn('echo "codex_status=$codex_status" >> "$GITHUB_OUTPUT"', workflow)
        self.assertIn('if: ${{ always() && !cancelled() && steps.gate.outputs.should_run == \'true\' }}', workflow)
        self.assertIn('if: ${{ always() && !cancelled() && steps.gate.outputs.should_run == \'true\' && steps.state.outputs.changed == \'true\' }}', workflow)
        self.assertIn('if [[ "${CODEX_STATUS:-1}" != "0" ]]; then', workflow)

    def test_command_timeout_still_persists_state_before_final_failure(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "codex-window-primer.yml").read_text(encoding="utf-8")
        codex_step = workflow[workflow.index("- name: Run one quiet Codex request"):workflow.index("- name: Re-encrypt refreshed state without GitHub token")]
        self.assertIn("timeout --signal=TERM --kill-after=15s 3m", codex_step)
        self.assertLess(codex_step.index("timeout --signal=TERM --kill-after=15s 3m"), codex_step.index("env -i"))
        self.assertLess(codex_step.index("env -i"), codex_step.index("codex exec"))
        self.assertIn('echo "codex_status=$codex_status" >> "$GITHUB_OUTPUT"', codex_step)
        self.assertNotIn('exit "$codex_status"', codex_step)
        self.assertIn('if: ${{ always() && !cancelled() && steps.gate.outputs.should_run == \'true\' }}', workflow)
        self.assertIn('if [[ "${CODEX_STATUS:-1}" != "0" ]]; then', workflow)


if __name__ == "__main__":
    unittest.main()
