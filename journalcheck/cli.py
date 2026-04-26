from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from journalcheck.bootstrap import bootstrap_vendor

bootstrap_vendor()

from journalcheck.notifications import email_configured, send_test_email
from journalcheck.service import format_change_for_display, refresh_statuses
from journalcheck.storage import describe_change
from journalcheck.utils import load_env

ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ANSI_SUPPORTED = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch manuscript review statuses from AHA / EM / BMC.")
    parser.add_argument(
        "--site",
        action="append",
        choices=["aha", "em", "bmc"],
        help="Only run one or more selected site families. Default: all configured families.",
    )
    parser.add_argument("--once", action="store_true", help="Run one refresh and exit.")
    parser.add_argument("--interval-minutes", type=int, help="Run repeatedly with the given interval in minutes.")
    parser.add_argument("--test-email", action="store_true", help="Send a test email and exit.")
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not update the JSON/CSV files in the output directory.",
    )
    return parser.parse_args()


def enable_ansi_colors() -> None:
    global ANSI_SUPPORTED
    if os.name != "nt":
        ANSI_SUPPORTED = True
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if handle in (0, -1):
            return
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return
        if kernel32.SetConsoleMode(handle, mode.value | 0x0004):
            ANSI_SUPPORTED = True
    except Exception:
        return


def color_red(text: str) -> str:
    if not ANSI_SUPPORTED:
        return text
    return f"{ANSI_RED}{text}{ANSI_RESET}"


def build_change_map(changes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        change["key"]: change
        for change in changes
        if change.get("key") and change.get("type") != "no_longer_active"
    }


def print_change_section(changes: list[dict[str, Any]] | None) -> None:
    print("Change Status:")
    if changes is None:
        print("- Tracking skipped (--no-save)")
        print()
        return
    if not changes:
        print("- [NO CHANGE]")
        print()
        return
    print(color_red(f"- [CHANGED] ({len(changes)})"))
    for change in changes:
        title = change.get("title") or "-"
        site = change.get("site") or "-"
        manuscript_number = change.get("manuscript_number") or "-"
        print(f"  [{site}] {manuscript_number} | {title}")
        print(f"    {format_change_for_display(change)}")
    print()


def print_summary(rows: list, change_map: dict[str, dict[str, Any]] | None = None) -> None:
    if not rows:
        print("No active submission statuses found.")
        return
    counts = Counter(row.site for row in rows)
    print("Fetched active statuses:")
    for site, count in sorted(counts.items()):
        print(f"- {site}: {count}")
    print()
    for row in rows:
        prefix = color_red("[CHANGED] ") if change_map and row.key() in change_map else ""
        print(f"{prefix}[{row.site}] {row.manuscript_number} | {row.status}")
        print(f"  Source: {row.source}")
        if row.title:
            print(f"  Title: {row.title}")
        if change_map and row.key() in change_map:
            print(f"  Change: {format_change_for_display(change_map[row.key()])}")
        if row.submission_date:
            print(f"  Submission Date: {row.submission_date}")
        if row.status_date:
            print(f"  Status Date: {row.status_date}")
        reviewer_values = [
            row.reviewer_value_display("reviewer_invited"),
            row.reviewer_value_display("reviewer_accepted"),
            row.reviewer_value_display("review_reports_received"),
        ]
        if any(value != "-" for value in reviewer_values):
            print(
                "  Reviewer Stats: "
                f"invited={reviewer_values[0]}, "
                f"accepted={reviewer_values[1]}, "
                f"reports={reviewer_values[2]}"
            )
        if row.review_comments:
            print(f"  Review Comments: {len(row.review_comments)} block(s) captured")
        if row.notes:
            print(f"  Notes: {' | '.join(row.notes)}")
        print()


def run_refresh(root: Path, selected: set[str] | None, save: bool) -> list:
    result = refresh_statuses(root=root, selected=selected, save=save, notify=True)
    changes = result.changes if save else None
    print_change_section(changes)
    if result.warnings:
        print("Warnings:")
        for item in result.warnings:
            print(f"- {item}")
        print()
    print_summary(result.rows, build_change_map(result.changes))

    if save and result.json_path is not None and result.csv_path is not None and result.log_path is not None:
        print(f"Updated JSON: {result.json_path}")
        print(f"Updated CSV:  {result.csv_path}")
        print(f"Refresh Log:  {result.log_path}")
        if result.changes:
            print(f"Detected changes: {len(result.changes)}")
            if result.notifications_sent:
                print(f"Notification sent via: {', '.join(result.notifications_sent)}")
            elif not result.warnings:
                print("Notifications not sent: no notification channel configured.")
    return result.rows


def run_email_test() -> int:
    if not email_configured():
        print("Email is not fully configured in .env")
        return 1
    try:
        send_test_email()
    except Exception as exc:
        print(f"Test email failed: {exc}")
        return 1
    print("Test email sent successfully.")
    return 0


def choose_menu_mode() -> int | None:
    print("Choose run mode:")
    print("1. Run once")
    print("2. Auto refresh every 30 minutes")
    print("3. Auto refresh every 60 minutes")
    print("4. Auto refresh with custom minutes")
    print("5. Test email sending")
    print("0. Exit")
    while True:
        choice = input("Enter choice: ").strip()
        if choice in {"0", "1", "2", "3", "4", "5"}:
            return int(choice)
        print("Invalid choice. Try again.")


def run_loop(root: Path, selected: set[str] | None, interval_minutes: int, save: bool) -> int:
    if interval_minutes <= 0:
        raise ValueError("Interval minutes must be positive.")
    print(f"Auto refresh started. Interval: {interval_minutes} minute(s). Press Ctrl+C to stop.")
    try:
        while True:
            started_at = datetime.now().astimezone()
            print()
            print(f"[{started_at.strftime('%Y-%m-%d %H:%M:%S')}] Refresh started...")
            try:
                run_refresh(root, selected, save)
            except Exception as exc:
                print(f"Refresh failed: {exc}")
            next_run = datetime.now().astimezone() + timedelta(minutes=interval_minutes)
            print(f"Next refresh: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
            time.sleep(interval_minutes * 60)
    except KeyboardInterrupt:
        print("\nAuto refresh stopped.")
        return 0


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parent.parent
    load_env(root)
    enable_ansi_colors()

    selected = set(args.site) if args.site else None
    save = not args.no_save

    if args.test_email:
        return run_email_test()
    if args.interval_minutes is not None:
        return run_loop(root, selected, args.interval_minutes, save)

    if args.once or not sys.stdin.isatty():
        run_refresh(root, selected, save)
        return 0

    choice = choose_menu_mode()
    if choice == 0 or choice is None:
        return 0
    if choice == 1:
        run_refresh(root, selected, save)
        return 0
    if choice == 2:
        return run_loop(root, selected, 30, save)
    if choice == 3:
        return run_loop(root, selected, 60, save)
    if choice == 4:
        while True:
            raw = input("Enter refresh interval in minutes: ").strip()
            if raw.isdigit() and int(raw) > 0:
                return run_loop(root, selected, int(raw), save)
            print("Please enter an integer greater than 0.")
    if choice == 5:
        return run_email_test()
    return 0
