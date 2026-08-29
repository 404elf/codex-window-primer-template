#!/usr/bin/env python3
"""Small, credential-free controller for the Codex window schedule.

The controller edits only schedule.json and the marked GitHub cron line. It
never reads auth.json, AGE_PRIVATE_KEY, or any other credential material.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "schedule.json"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "codex-window-primer.yml"
INERT_CRON = "0 0 1 1 *"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_WINDOW_MINUTES = 300
DEFAULT_RESET_MINUTES = 90


class ControlError(Exception):
    """A safe, user-facing controller error."""


def command(argv: list[str], *, check: bool = True) -> str:
    """Run a local control command without exposing its output on failure."""

    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise ControlError(f"Required local tool is unavailable: {argv[0]}") from exc
    if check and result.returncode != 0:
        raise ControlError(f"Control command failed: {argv[0]}")
    return result.stdout.strip()


def parse_duration(value: str) -> int:
    """Parse a compact duration such as 5h, 90m, or 1h30m into minutes."""

    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", value.strip().lower())
    if not match or (match.group(1) is None and match.group(2) is None):
        raise ControlError(f"Invalid duration: {value!r}; use forms such as 1h30m or 90m")
    minutes = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
    if minutes < 0:
        raise ControlError("Duration must not be negative")
    return minutes


def parse_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoError differs between Python versions.
        if name == "Asia/Shanghai":
            # Windows Python installations often have no system tzdata. Beijing
            # has a fixed UTC+8 offset, so this fallback is deterministic.
            return timezone(timedelta(hours=8), name)
        raise ControlError(f"Unknown IANA timezone: {name}") from exc


def parse_local_datetime(value: str, timezone_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControlError("Work time must be ISO format, for example 2030-01-02T10:00") from exc
    zone = parse_timezone(timezone_name)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ControlError(f"Invalid date: {value}") from exc


def load_plan() -> dict:
    try:
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError("schedule.json cannot be read") from exc
    if not isinstance(plan, dict):
        raise ControlError("schedule.json must contain an object")
    return plan


def timing(plan: dict) -> tuple[tzinfo, datetime, timedelta, datetime]:
    timezone_name = str(plan.get("timezone", DEFAULT_TIMEZONE))
    zone = parse_timezone(timezone_name)
    work_start = parse_local_datetime(str(plan["work_start_local"]), timezone_name)
    window_minutes = int(plan.get("window_duration_minutes", DEFAULT_WINDOW_MINUTES))
    reset_minutes = int(plan.get("reset_after_start_minutes", DEFAULT_RESET_MINUTES))
    if window_minutes <= 0 or reset_minutes < 0 or reset_minutes >= window_minutes:
        raise ControlError("window_duration_minutes/reset_after_start_minutes are invalid")
    lead = timedelta(minutes=window_minutes - reset_minutes)
    return zone, work_start, lead, work_start - lead


def cron_for(plan: dict) -> str:
    if not plan.get("enabled", False):
        return INERT_CRON
    _, _, _, primer = timing(plan)
    utc_primer = primer.astimezone(timezone.utc)
    minute = utc_primer.minute
    hour = utc_primer.hour
    mode = plan.get("mode", "once")
    if mode == "once":
        return f"{minute} {hour} {utc_primer.day} {utc_primer.month} *"
    if mode == "daily":
        return f"{minute} {hour} * * *"
    if mode == "weekly":
        github_weekday = (utc_primer.weekday() + 1) % 7
        return f"{minute} {hour} * * {github_weekday}"
    raise ControlError(f"Unsupported schedule mode: {mode}")


def replace_cron(workflow: str, cron: str) -> str:
    pattern = re.compile(
        r'(?m)^(?P<indent>[ \t]*)# codex-window-primer: cron-managed[^\r\n]*\r?\n'
        r'(?P<cron_indent>[ \t]*)- cron: "[^"\r\n]*"\r?\n'
    )
    replacement = (
        "{indent}# codex-window-primer: cron-managed\n"
        "{cron_indent}- cron: \"{cron}\"\n"
    ).format(indent="{indent}", cron_indent="{cron_indent}", cron=cron)

    def repl(match: re.Match[str]) -> str:
        return replacement.format(
            indent=match.group("indent"), cron_indent=match.group("cron_indent")
        )

    updated, count = pattern.subn(repl, workflow)
    if count != 1:
        raise ControlError("Expected exactly one managed GitHub cron line")
    return updated


def staged_paths() -> list[str]:
    output = command(["git", "diff", "--cached", "--name-only"])
    return [line for line in output.splitlines() if line]


def repository_slug() -> str:
    remote = command(["git", "config", "--get", "remote.origin.url"])
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    if not match:
        raise ControlError("The repository origin is not a GitHub repository")
    return f"{match.group(1)}/{match.group(2)}"


def verify_remote() -> tuple[str, str, str]:
    branch = command(["git", "branch", "--show-current"])
    local_sha = command(["git", "rev-parse", "HEAD"])
    remote_line = command(["git", "ls-remote", "origin", f"refs/heads/{branch}"])
    remote_sha = remote_line.split()[0] if remote_line else ""
    if not branch or not remote_sha or local_sha != remote_sha:
        raise ControlError("The pushed branch could not be verified")

    slug = repository_slug()
    private = command(["gh", "api", f"repos/{slug}", "--jq", ".private"]).lower()
    state = command(
        [
            "gh",
            "api",
            f"repos/{slug}/actions/workflows/codex-window-primer.yml",
            "--jq",
            ".state",
        ]
    ).lower()
    if private != "true":
        raise ControlError("The runtime repository must be private")
    if state != "active":
        raise ControlError("The GitHub Actions workflow is not active")
    return slug, branch, local_sha


def write_plan(plan: dict) -> None:
    PLAN_PATH.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def save_and_push(plan: dict, cron: str, message: str, *, dry_run: bool) -> None:
    if dry_run:
        print("Dry run; no local or remote files changed.")
        return

    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    updated_workflow = replace_cron(workflow, cron)
    write_plan(plan)
    WORKFLOW_PATH.write_text(updated_workflow, encoding="utf-8")
    command(["git", "diff", "--check"])

    existing = staged_paths()
    managed = {"schedule.json", ".github/workflows/codex-window-primer.yml"}
    if any(path not in managed for path in existing):
        raise ControlError("Unrelated staged changes were found; refusing to commit them")
    command(["git", "add", "--", "schedule.json", ".github/workflows/codex-window-primer.yml"])
    staged = staged_paths()
    if any(path not in managed for path in staged):
        raise ControlError("The controller staged an unexpected file")
    if not staged:
        verify_remote()
        print("No schedule change was needed; remote workflow verified.")
        return

    command(["git", "commit", "-m", message])
    branch = command(["git", "branch", "--show-current"])
    command(["git", "push", "origin", f"HEAD:{branch}"])
    slug, verified_branch, sha = verify_remote()
    print(f"Remote verified: private repository {slug}, workflow active, branch {verified_branch} at {sha[:12]}.")


def set_plan(args: argparse.Namespace) -> None:
    old = load_plan()
    timezone_name = args.timezone or old.get("timezone", DEFAULT_TIMEZONE)
    parse_timezone(timezone_name)
    mode = args.mode or old.get("mode", "once")
    if mode not in {"once", "daily", "weekly"}:
        raise ControlError("mode must be once, daily, or weekly")

    work_start = parse_local_datetime(args.work_start, timezone_name)
    window_minutes = (
        parse_duration(args.window_duration)
        if args.window_duration
        else int(old.get("window_duration_minutes", DEFAULT_WINDOW_MINUTES))
    )
    reset_minutes = (
        parse_duration(args.reset_after)
        if args.reset_after
        else int(old.get("reset_after_start_minutes", DEFAULT_RESET_MINUTES))
    )
    if window_minutes <= 0 or reset_minutes < 0 or reset_minutes >= window_minutes:
        raise ControlError("reset-after must be less than the positive window duration")

    if args.active_from:
        active_from = parse_date(args.active_from)
    elif mode in {"daily", "weekly"} and old.get("active_from_local"):
        active_from = parse_date(old["active_from_local"])
    else:
        active_from = work_start.date()

    if args.active_until:
        active_until = parse_date(args.active_until)
    elif mode == "once":
        active_until = work_start.date()
    elif mode in {"daily", "weekly"} and old.get("active_until_local"):
        active_until = parse_date(old["active_until_local"])
    else:
        active_until = None

    if active_until and active_until < active_from:
        raise ControlError("active-until cannot be before active-from")

    plan = {
        "version": 1,
        "enabled": True,
        "mode": mode,
        "timezone": timezone_name,
        "window_duration_minutes": window_minutes,
        "reset_after_start_minutes": reset_minutes,
        "work_start_local": work_start.isoformat(timespec="minutes"),
        "active_from_local": active_from.isoformat(),
        "active_until_local": active_until.isoformat() if active_until else None,
        "skip_dates_local": [],
    }
    _, _, lead, primer = timing(plan)
    cron = cron_for(plan)
    save_and_push(
        plan,
        cron,
        f"chore: update Codex work schedule ({primer.isoformat(timespec='minutes')})",
        dry_run=args.dry_run,
    )
    print(f"Work start: {work_start.isoformat(timespec='minutes')}")
    print(f"Prime time: {primer.isoformat(timespec='minutes')} ({lead.total_seconds() / 3600:g}h before work)")
    print(f"GitHub Actions cron (UTC): {cron}")


def cancel_plan(args: argparse.Namespace) -> None:
    plan = load_plan()
    _, work_start, _, _ = timing(plan)
    timezone_name = str(plan.get("timezone", DEFAULT_TIMEZONE))
    if args.date is None:
        plan["enabled"] = False
        cron = INERT_CRON
        message = "chore: pause Codex window schedule"
        note = "The schedule is paused."
    else:
        cancel_date = parse_date(args.date)
        if plan.get("mode") == "once":
            if cancel_date != work_start.date():
                raise ControlError("The date does not match the one-time work plan")
            plan["enabled"] = False
            cron = INERT_CRON
            message = "chore: cancel Codex window schedule"
            note = f"The one-time plan for {cancel_date} is canceled."
        else:
            skipped = set(plan.get("skip_dates_local", []))
            skipped.add(cancel_date.isoformat())
            plan["skip_dates_local"] = sorted(skipped)
            cron = cron_for(plan)
            message = f"chore: skip Codex window on {cancel_date.isoformat()}"
            note = f"The {cancel_date} occurrence is skipped; future recurring occurrences remain enabled."

    save_and_push(plan, cron, message, dry_run=args.dry_run)
    print(note)
    if args.date is None:
        print(f"Timezone remains {timezone_name}; manual dispatch is also blocked while disabled.")


def set_enabled(args: argparse.Namespace, enabled: bool) -> None:
    plan = load_plan()
    plan["enabled"] = enabled
    cron = cron_for(plan)
    _, _, _, primer = timing(plan)
    action = "resume" if enabled else "pause"
    save_and_push(plan, cron, f"chore: {action} Codex window schedule", dry_run=args.dry_run)
    print(f"Schedule {'enabled' if enabled else 'paused'}.")
    if enabled:
        print(f"Computed prime time: {primer.isoformat(timespec='minutes')}; UTC cron: {cron}")


def remote_status() -> None:
    slug, branch, sha = verify_remote()
    secret_check = subprocess.run(
        ["gh", "api", f"repos/{slug}/actions/secrets/AGE_PRIVATE_KEY", "--jq", ".name"],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    bundle_check = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{slug}/contents/auth.json.enc?ref={branch}",
            "--jq",
            ".name",
        ],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    print(f"Remote: private repository {slug}; workflow active; branch {branch} at {sha[:12]}.")
    print(f"AGE_PRIVATE_KEY secret: {'configured' if secret_check.returncode == 0 else 'not detected'}.")
    print(f"Encrypted auth bundle: {'present' if bundle_check.returncode == 0 else 'not detected'}.")


def show_status(args: argparse.Namespace) -> None:
    plan = load_plan()
    _, work_start, lead, primer = timing(plan)
    print(f"Enabled: {str(bool(plan.get('enabled', False))).lower()}")
    print(f"Mode: {plan.get('mode', 'unknown')}")
    print(f"Work start: {work_start.isoformat(timespec='minutes')}")
    print(f"Prime time: {primer.isoformat(timespec='minutes')}")
    print(f"Reset after work start: {plan.get('reset_after_start_minutes', DEFAULT_RESET_MINUTES)} minutes")
    print(f"Lead before work: {lead.total_seconds() / 3600:g} hours")
    print(f"UTC cron: {cron_for(plan)}")
    skipped = plan.get("skip_dates_local", [])
    print(f"Skipped dates: {', '.join(skipped) if skipped else 'none'}")
    if not args.offline:
        remote_status()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the Codex window primer without editing YAML.")
    parser.add_argument("--offline", action="store_true", help="Skip GitHub verification; intended for local inspection only.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    set_parser = subparsers.add_parser("set", help="Set a work schedule and publish its computed cron.")
    set_parser.add_argument("--work-start", required=True, help="ISO local time, with optional offset.")
    set_parser.add_argument("--reset-after", help="Time from work start to desired reset, e.g. 1h30m.")
    set_parser.add_argument("--window-duration", help="Usage window duration, default 5h.")
    set_parser.add_argument("--timezone", help="IANA timezone, default Asia/Shanghai.")
    set_parser.add_argument("--mode", choices=["once", "daily", "weekly"])
    set_parser.add_argument("--active-from")
    set_parser.add_argument("--active-until")
    set_parser.add_argument("--dry-run", action="store_true")
    set_parser.set_defaults(handler=set_plan)

    cancel_parser = subparsers.add_parser("cancel", help="Cancel one occurrence or pause a one-time plan.")
    cancel_parser.add_argument("--date", help="Beijing work date to skip; omit to pause the whole plan.")
    cancel_parser.add_argument("--dry-run", action="store_true")
    cancel_parser.set_defaults(handler=cancel_plan)

    for name, enabled in (("pause", False), ("resume", True)):
        toggle = subparsers.add_parser(name)
        toggle.add_argument("--dry-run", action="store_true")
        toggle.set_defaults(handler=lambda args, value=enabled: set_enabled(args, value))

    status_parser = subparsers.add_parser("status", help="Show the plan and verify the remote workflow.")
    status_parser.set_defaults(handler=show_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.handler(args)
    except (ControlError, KeyError, TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
