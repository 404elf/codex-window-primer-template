import json
import re
import unittest
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest.mock import patch

from schedule_logic import (
    INERT_CRON,
    cron_entries,
    cron_for,
    due_slots,
    due_window,
    prime_time,
    today_prime_action,
    validate_plan,
)


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


def v2_plan(**overrides):
    value = {
        "version": 2,
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "window_duration_minutes": 300,
        "reset_after_start_minutes": 90,
        "active_from_local": "2030-01-01",
        "active_until_local": None,
        "weekly": {},
        "dates": {},
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
        matches = re.findall(
            r'^[ \t]*- cron: "([^"]+)"\r?\n[ \t]+timezone: "([^"]+)"',
            workflow,
            re.MULTILINE,
        )
        self.assertEqual(
            matches,
            [(entry, schedule["timezone"]) for entry in cron_entries(schedule)],
        )

    def test_v2_daily_two_slots(self):
        value = v2_plan(
            weekly={str(index): ["09:00", "20:00"] for index in range(7)},
        )
        self.assertEqual(cron_entries(value), ("30 5 * * *", "30 16 * * *"))
        morning, _ = due_window(
            value,
            datetime(2030, 1, 7, 5, 30, tzinfo=BEIJING),
            wakeup_schedule="30 5 * * *",
        )
        evening, _ = due_window(
            value,
            datetime(2030, 1, 7, 16, 30, tzinfo=BEIJING),
            wakeup_schedule="30 16 * * *",
        )
        self.assertTrue(morning)
        self.assertTrue(evening)

    def test_v2_weekdays_at_nine(self):
        value = v2_plan(weekly={str(index): ["09:00"] for index in range(5)})
        self.assertEqual(cron_entries(value), ("30 5 * * 1,2,3,4,5",))
        monday, _ = due_window(
            value,
            datetime(2030, 1, 7, 5, 30, tzinfo=BEIJING),
            wakeup_schedule="30 5 * * 1,2,3,4,5",
        )
        saturday, _ = due_window(
            value,
            datetime(2030, 1, 12, 5, 30, tzinfo=BEIJING),
            wakeup_schedule="30 5 * * 1,2,3,4,5",
        )
        self.assertTrue(monday)
        self.assertFalse(saturday)

    def test_v2_weekends_at_eleven(self):
        value = v2_plan(weekly={"5": ["11:00"], "6": ["11:00"]})
        self.assertEqual(cron_entries(value), ("30 7 * * 0,6",))
        due, _ = due_window(
            value,
            datetime(2030, 1, 6, 7, 30, tzinfo=BEIJING),
            wakeup_schedule="30 7 * * 0,6",
        )
        self.assertTrue(due)

    def test_v2_specific_weekday(self):
        value = v2_plan(weekly={"2": ["20:00"]})
        self.assertEqual(cron_entries(value), ("30 16 * * 3",))

    def test_v2_date_override_replaces_weekly(self):
        value = v2_plan(
            weekly={"0": ["09:00"]},
            dates={"2030-01-07": {"mode": "override", "slots": ["14:00"]}},
        )
        self.assertEqual(cron_entries(value), ("30 5 * * 1", "30 10 7 1 *"))
        old_wakeup, _ = due_window(
            value,
            datetime(2030, 1, 7, 5, 30, tzinfo=BEIJING),
            wakeup_schedule="30 5 * * 1",
        )
        new_wakeup, _ = due_window(
            value,
            datetime(2030, 1, 7, 10, 30, tzinfo=BEIJING),
            wakeup_schedule="30 10 7 1 *",
        )
        self.assertFalse(old_wakeup)
        self.assertTrue(new_wakeup)

    def test_v2_date_extra_appends_slot(self):
        value = v2_plan(
            weekly={"0": ["09:00"]},
            dates={"2030-01-07": {"mode": "extra", "slots": ["14:00"]}},
        )
        self.assertEqual(len(cron_entries(value)), 2)
        due = due_slots(
            value,
            datetime(2030, 1, 7, 10, 30, tzinfo=BEIJING),
            wakeup_schedule="30 10 7 1 *",
        )
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0][0].spec.clock.hour, 14)

    def test_today_temporary_prime_not_yet_uses_normal_cron(self):
        value = v2_plan(
            dates={"2030-01-07": {"mode": "override", "slots": ["14:00"]}},
        )
        now = datetime(2030, 1, 7, 9, 0, tzinfo=BEIJING)
        self.assertEqual(today_prime_action(value, now), "schedule")
        self.assertEqual(cron_entries(value, now=now), ("30 10 7 1 *",))

    def test_today_temporary_prime_already_passed_uses_dispatch_path(self):
        value = v2_plan(
            dates={"2030-01-07": {"mode": "override", "slots": ["14:00"]}},
        )
        now = datetime(2030, 1, 7, 11, 0, tzinfo=BEIJING)
        self.assertEqual(today_prime_action(value, now), "dispatch")
        self.assertEqual(cron_entries(value, now=now), (INERT_CRON,))

    def test_v2_date_cancel_day(self):
        value = v2_plan(
            weekly={"0": ["09:00"]},
            dates={"2030-01-07": {"mode": "cancel"}},
        )
        due, _ = due_window(
            value,
            datetime(2030, 1, 7, 5, 30, tzinfo=BEIJING),
            wakeup_schedule="30 5 * * 1",
        )
        self.assertFalse(due)
        self.assertEqual(cron_entries(value), ("30 5 * * 1",))

    def test_v2_date_cancel_single_slot(self):
        value = v2_plan(
            weekly={"0": ["09:00", "20:00"]},
            dates={"2030-01-07": {"mode": "cancel", "slots": ["09:00"]}},
        )
        morning, _ = due_window(
            value,
            datetime(2030, 1, 7, 5, 30, tzinfo=BEIJING),
            wakeup_schedule="30 5 * * 1",
        )
        evening, _ = due_window(
            value,
            datetime(2030, 1, 7, 16, 30, tzinfo=BEIJING),
            wakeup_schedule="30 16 * * 1",
        )
        self.assertFalse(morning)
        self.assertTrue(evening)

    def test_v2_one_off_expires(self):
        value = v2_plan(
            dates={"2030-01-03": {"mode": "override", "slots": ["14:00"]}},
        )
        self.assertEqual(cron_entries(value), ("30 10 3 1 *",))
        active, _ = due_window(
            value,
            datetime(2030, 1, 3, 10, 30, tzinfo=BEIJING),
            wakeup_schedule="30 10 3 1 *",
        )
        expired, _ = due_window(
            value,
            datetime(2030, 1, 4, 10, 30, tzinfo=BEIJING),
            wakeup_schedule="30 10 3 1 *",
        )
        self.assertTrue(active)
        self.assertFalse(expired)

    def test_v2_prime_crosses_midnight(self):
        value = v2_plan(
            weekly={"0": [{"time": "02:00", "reset_after_start_minutes": 120}]},
        )
        self.assertEqual(cron_entries(value), ("0 23 * * 0",))
        due, _ = due_window(
            value,
            datetime(2030, 1, 6, 23, 0, tzinfo=BEIJING),
            wakeup_schedule="0 23 * * 0",
        )
        self.assertTrue(due)

    def test_v2_weekly_midnight_prime_keeps_timezone(self):
        value = v2_plan(
            timezone="America/New_York",
            weekly={"0": [{"time": "02:00", "reset_after_start_minutes": 120}]},
        )
        with patch("schedule_logic._timezone", return_value=NEW_YORK):
            timing = validate_plan(value)
            self.assertEqual(timing.primer.date(), date(2030, 1, 6))
            self.assertEqual((timing.primer.hour, timing.primer.minute), (23, 0))
            self.assertEqual(timing.primer.tzinfo, NEW_YORK)
            self.assertEqual(cron_entries(value), ("0 23 * * 0",))

    def test_v2_weekly_multi_slot_with_slot_reset_override(self):
        value = v2_plan(
            weekly={
                "0": ["09:00", {"time": "20:00", "reset_after_start_minutes": 120}],
                "2": ["09:00"],
            },
        )
        self.assertEqual(
            cron_entries(value),
            ("30 5 * * 1,3", "0 17 * * 1"),
        )

    def test_v2_new_york_dst_keeps_local_prime_time(self):
        value = v2_plan(
            timezone="America/New_York",
            weekly={str(index): ["09:00"] for index in range(7)},
        )
        with patch("schedule_logic._timezone", return_value=NEW_YORK):
            before, _ = due_window(
                value,
                datetime(2030, 3, 8, 5, 30, tzinfo=NEW_YORK),
                wakeup_schedule="30 5 * * *",
            )
            after, _ = due_window(
                value,
                datetime(2030, 3, 11, 5, 30, tzinfo=NEW_YORK),
                wakeup_schedule="30 5 * * *",
            )
            self.assertEqual(cron_entries(value), ("30 5 * * *",))
        self.assertTrue(before)
        self.assertTrue(after)

    def test_v2_multiple_wakeups_do_not_duplicate_same_slot(self):
        value = v2_plan(
            weekly={str(index): ["09:00", "20:00"] for index in range(7)},
        )
        entries = cron_entries(value)
        self.assertEqual(len(entries), len(set(entries)))
        matching = due_slots(
            value,
            datetime(2030, 1, 7, 5, 30, tzinfo=BEIJING),
            wakeup_schedule=entries[0],
        )
        nonmatching = due_slots(
            value,
            datetime(2030, 1, 7, 5, 30, tzinfo=BEIJING),
            wakeup_schedule=entries[1],
        )
        self.assertEqual(len(matching), 1)
        self.assertEqual(len(nonmatching), 0)

    def test_append_and_replace_have_distinct_slot_semantics(self):
        appended = v2_plan(
            weekly={"0": ["09:00"]},
            dates={"2030-01-07": {"mode": "extra", "slots": ["20:00"]}},
        )
        replaced = v2_plan(
            weekly={"0": ["09:00"]},
            dates={"2030-01-07": {"mode": "override", "slots": ["20:00"]}},
        )
        appended_slots = due_slots(
            appended,
            datetime(2030, 1, 7, 5, 30, tzinfo=BEIJING),
        ) + due_slots(
            appended,
            datetime(2030, 1, 7, 16, 30, tzinfo=BEIJING),
        )
        replaced_slots = due_slots(
            replaced,
            datetime(2030, 1, 7, 16, 30, tzinfo=BEIJING),
        )
        self.assertEqual({slot.spec.clock.hour for slot, _ in appended_slots}, {9, 20})
        self.assertEqual({slot.spec.clock.hour for slot, _ in replaced_slots}, {20})

    def test_v1_migrates_to_equivalent_v2_schedule(self):
        old = plan(mode="daily", active_until_local=None)
        new = v2_plan(
            active_from_local="2030-01-02",
            weekly={str(index): ["10:00"] for index in range(7)},
        )
        self.assertEqual(prime_time(old), prime_time(new))
        self.assertEqual(cron_entries(old), cron_entries(new))
        old_due, _ = due_window(old, datetime(2030, 1, 3, 6, 30, tzinfo=BEIJING))
        new_due, _ = due_window(new, datetime(2030, 1, 3, 6, 30, tzinfo=BEIJING))
        self.assertEqual(old_due, new_due)

    def test_workflow_uses_event_cron_as_schedule_gate(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "codex-window-primer.yml").read_text(encoding="utf-8")
        self.assertIn("WAKEUP_SCHEDULE: ${{ github.event.schedule }}", workflow)
        self.assertIn("wakeup_schedule=os.environ.get(\"WAKEUP_SCHEDULE\") or None", workflow)

    def test_oauth_and_workflow_security_invariants_remain(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "codex-window-primer.yml").read_text(encoding="utf-8")
        trigger_block = workflow[:workflow.index("permissions:")]
        self.assertIn("schedule:", trigger_block)
        self.assertIn("workflow_dispatch:", trigger_block)
        self.assertNotIn("pull_request", trigger_block)
        self.assertNotIn("issues:", trigger_block)
        self.assertNotIn("push:", trigger_block)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("concurrency:", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertEqual(workflow.count("AGE_PRIVATE_KEY: ${{ secrets.AGE_PRIVATE_KEY }}"), 1)
        codex_step = workflow[workflow.index("- name: Run one quiet Codex request"):workflow.index("- name: Re-encrypt refreshed state without GitHub token")]
        self.assertNotIn("AGE_PRIVATE_KEY", codex_step)
        self.assertIn('env -i \\\n', codex_step)
        self.assertIn('include_only = ["CODEX_HOME", "HOME", "PATH", "TMPDIR", "LANG", "LC_*", "TERM"]', workflow)

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
