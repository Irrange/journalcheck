from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from journalcheck.config import NETWORK_KEYS, OUTPUT_KEYS, collect_site_configs, require_site_configs
from journalcheck.models import SubmissionStatus
from journalcheck.notifications import send_notifications
from journalcheck.sites import AHAChecker, BMCChecker, EditorialManagerChecker
from journalcheck.storage import describe_change, update_store
from journalcheck.utils import ensure_output_dir, is_active_status


@dataclass(slots=True)
class RefreshResult:
    rows: list[SubmissionStatus]
    changes: list[dict[str, Any]]
    refreshed_at: str
    output_dir: Path | None = None
    json_path: Path | None = None
    csv_path: Path | None = None
    log_path: Path | None = None
    notifications_sent: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _env_get(env_map: Mapping[str, str] | None, key: str, default: str) -> str:
    if env_map is None:
        value = os.getenv(key, "").strip()
    else:
        value = env_map.get(key, "").strip()
    return value or default


@contextmanager
def patched_environment(env_map: Mapping[str, str] | None):
    if env_map is None:
        yield
        return

    managed_keys = {
        key
        for key in set(os.environ) | set(env_map)
        if key.startswith(("AHA_", "EM_", "BMC_", "SMTP_", "WECOM_")) or key in OUTPUT_KEYS or key in NETWORK_KEYS
    }
    snapshot = {key: os.environ.get(key) for key in managed_keys}
    for key in managed_keys:
        os.environ.pop(key, None)
    for key, value in env_map.items():
        if key in managed_keys and value.strip():
            os.environ[key] = value.strip()
    try:
        yield
    finally:
        for key in managed_keys:
            os.environ.pop(key, None)
        for key, value in snapshot.items():
            if value is not None:
                os.environ[key] = value


def build_checkers(selected: set[str] | None = None, env_map: Mapping[str, str] | None = None) -> list:
    site_order = ["aha", "em", "bmc"]
    requested = site_order if selected is None else [site for site in site_order if site in selected]
    checkers: list = []

    for site_key in requested:
        configs = collect_site_configs(site_key, env_map=env_map)
        if not configs:
            if selected is not None:
                configs = require_site_configs(site_key, env_map=env_map)
            else:
                continue

        if site_key == "aha":
            for site_name, config in configs:
                checkers.append(
                    AHAChecker(
                        base_url=config["base_url"],
                        username=config["username"],
                        password=config["password"],
                        site_name=site_name,
                    )
                )
        elif site_key == "em":
            for site_name, config in configs:
                checkers.append(
                    EditorialManagerChecker(
                        base_url=config["base_url"],
                        journal_code=config["journal_code"],
                        username=config["username"],
                        password=config["password"],
                        site_name=site_name,
                    )
                )
        elif site_key == "bmc":
            for site_name, config in configs:
                checkers.append(
                    BMCChecker(
                        submission_url=config["submission_url"],
                        username=config["username"],
                        password=config["password"],
                        site_name=site_name,
                    )
                )

    if not checkers:
        raise ValueError("No site accounts configured.")
    return checkers


def _checker_label(checker: object) -> str:
    return str(getattr(checker, "site_name", checker.__class__.__name__))


def _run_checker_with_retries(
    checker: object,
    attempts: int = 3,
    initial_delay_seconds: float = 3.0,
) -> tuple[list[SubmissionStatus], list[str], str | None]:
    warnings: list[str] = []
    delay_seconds = initial_delay_seconds
    label = _checker_label(checker)

    for attempt in range(1, attempts + 1):
        try:
            return list(checker.check()), warnings, None
        except Exception as exc:
            if attempt < attempts:
                warnings.append(
                    f"{label}: attempt {attempt}/{attempts} failed ({exc}). Retrying in {int(delay_seconds)}s."
                )
                time.sleep(delay_seconds)
                delay_seconds *= 2
                continue
            return [], warnings, f"{label}: failed after {attempts} attempt(s): {exc}"


def _preserve_failed_site_rows(
    root: Path,
    failed_sites: set[str],
    env_map: Mapping[str, str] | None,
) -> list[SubmissionStatus]:
    if not failed_sites:
        return []
    _, saved_rows = load_saved_rows(root, env_map=env_map)
    return [row for row in saved_rows if row.site in failed_sites]


def _preserve_unselected_site_rows(
    root: Path,
    selected: set[str] | None,
    env_map: Mapping[str, str] | None,
) -> list[SubmissionStatus]:
    if not selected:
        return []
    _, saved_rows = load_saved_rows(root, env_map=env_map)
    return [row for row in saved_rows if row.site.split("_", 1)[0] not in selected]


def _row_priority(row: SubmissionStatus) -> int:
    priority = 2 if is_active_status(row.status) else 0
    if str(row.metadata.get("folder", "")) == "Live Manuscripts":
        priority += 1
    return priority


def _dedupe_rows(rows: list[SubmissionStatus]) -> list[SubmissionStatus]:
    rows_by_key: dict[str, SubmissionStatus] = {}
    ordered_keys: list[str] = []
    for row in rows:
        key = row.key()
        if key not in rows_by_key:
            rows_by_key[key] = row
            ordered_keys.append(key)
            continue
        if _row_priority(row) > _row_priority(rows_by_key[key]):
            rows_by_key[key] = row
    return [rows_by_key[key] for key in ordered_keys]


def _merge_missing_progress_from_saved_rows(
    root: Path,
    rows: list[SubmissionStatus],
    env_map: Mapping[str, str] | None,
) -> None:
    if not rows:
        return
    saved_store = load_saved_store(root, env_map=env_map)
    saved_by_key = {record.get("key"): record for record in saved_store.get("records", []) if record.get("key")}
    display_keys = (
        "reviewer_invited_display",
        "reviewer_accepted_display",
        "review_reports_received_display",
    )
    stat_fields = (
        "reviewer_invited",
        "reviewer_accepted",
        "review_reports_received",
    )
    for row in rows:
        old = saved_by_key.get(row.key())
        if old is None:
            continue
        for field_name in stat_fields:
            if getattr(row, field_name) is not None:
                continue
            fallback_value = old.get(field_name)
            if fallback_value is None:
                for snapshot in reversed(list(old.get("history", []))):
                    fallback_value = snapshot.get(field_name)
                    if fallback_value is not None:
                        break
            if fallback_value is not None:
                setattr(row, field_name, fallback_value)
        old_metadata = old.get("metadata", {}) if isinstance(old.get("metadata"), dict) else {}
        for meta_key in display_keys:
            if row.metadata.get(meta_key) in (None, "") and old_metadata.get(meta_key) not in (None, ""):
                row.metadata[meta_key] = old_metadata[meta_key]


def refresh_statuses(
    root: Path,
    selected: set[str] | None = None,
    save: bool = True,
    notify: bool = True,
    env_map: Mapping[str, str] | None = None,
) -> RefreshResult:
    with patched_environment(env_map):
        collected_rows: list[SubmissionStatus] = []
        warnings: list[str] = []
        failed_sites: set[str] = set()
        for checker in build_checkers(selected=selected, env_map=env_map):
            checker_rows, checker_warnings, checker_error = _run_checker_with_retries(checker)
            warnings.extend(checker_warnings)
            collected_rows.extend(checker_rows)
            if checker_error is not None:
                warnings.append(checker_error)
                failed_sites.add(_checker_label(checker))

        collected_rows = _dedupe_rows(collected_rows)
        _merge_missing_progress_from_saved_rows(root, collected_rows, env_map=env_map)
        active_rows = [row for row in collected_rows if is_active_status(row.status)]
        inactive_rows = [row for row in collected_rows if not is_active_status(row.status)]

        refreshed_at = datetime.now().astimezone().isoformat(timespec="seconds")
        changes: list[dict[str, Any]] = []
        notifications_sent: list[str] = []
        output_dir: Path | None = None
        json_path: Path | None = None
        csv_path: Path | None = None
        log_path: Path | None = None
        rows_for_store = list(active_rows)

        if save:
            preserved_unselected_rows = _preserve_unselected_site_rows(root, selected, env_map=env_map)
            if preserved_unselected_rows:
                rows_for_store = _dedupe_rows(rows_for_store + preserved_unselected_rows)

            if failed_sites:
                preserved_rows = _preserve_failed_site_rows(root, failed_sites, env_map=env_map)
                if preserved_rows:
                    rows_for_store = _dedupe_rows(rows_for_store + preserved_rows)
                    warnings.append(
                        "Kept previous snapshot for failed site(s): " + ", ".join(sorted(failed_sites))
                    )

            output_dir = ensure_output_dir(root, _env_get(env_map, "OUTPUT_DIR", "outputs"))
            json_path = output_dir / _env_get(env_map, "STORE_JSON_NAME", "statuses.json")
            csv_path = output_dir / _env_get(env_map, "CURRENT_CSV_NAME", "statuses.csv")
            log_path = output_dir / _env_get(env_map, "REFRESH_LOG_NAME", "refresh_log.csv")
            _, changes = update_store(
                json_path,
                csv_path,
                log_path,
                rows_for_store,
                refreshed_at,
                inactive_rows=inactive_rows,
            )
            if notify and changes:
                try:
                    notifications_sent = send_notifications(changes, refreshed_at)
                except Exception as exc:
                    warnings.append(f"Notification failed: {exc}")

        return RefreshResult(
            rows=rows_for_store if save else active_rows,
            changes=changes,
            refreshed_at=refreshed_at,
            output_dir=output_dir,
            json_path=json_path,
            csv_path=csv_path,
            log_path=log_path,
            notifications_sent=notifications_sent,
            warnings=warnings,
        )


def format_change_for_display(change: dict[str, Any]) -> str:
    change_type = change.get("type")
    if change_type == "new_submission":
        return f"New submission: {change.get('new_status')}"
    if change_type == "status_changed":
        return f"Status changed: {change.get('old_status')} -> {change.get('new_status')}"
    if change_type == "progress_changed":
        return f"Progress changed: {change.get('summary')}"
    if change_type == "no_longer_active":
        return f"No longer active: {change.get('old_status')}"
    return describe_change(change)


def load_saved_store(root: Path, env_map: Mapping[str, str] | None = None) -> dict[str, Any]:
    output_dir = root / _env_get(env_map, "OUTPUT_DIR", "outputs")
    json_path = output_dir / _env_get(env_map, "STORE_JSON_NAME", "statuses.json")
    if not json_path.exists():
        return {"records": []}
    return json.loads(json_path.read_text(encoding="utf-8"))


def load_saved_rows(root: Path, env_map: Mapping[str, str] | None = None) -> tuple[str | None, list[SubmissionStatus]]:
    store = load_saved_store(root, env_map=env_map)
    rows: list[SubmissionStatus] = []
    for record in store.get("records", []):
        rows.append(
            SubmissionStatus(
                site=record.get("site", ""),
                source=record.get("source", ""),
                manuscript_number=record.get("manuscript_number", ""),
                title=record.get("title", ""),
                status=record.get("status", ""),
                status_date=record.get("status_date"),
                submission_date=record.get("submission_date"),
                detail_url=record.get("detail_url"),
                reviewer_invited=record.get("reviewer_invited"),
                reviewer_accepted=record.get("reviewer_accepted"),
                review_reports_received=record.get("review_reports_received"),
                review_comments_url=record.get("review_comments_url"),
                review_comments=list(record.get("review_comments", [])),
                notes=list(record.get("notes", [])),
                metadata=dict(record.get("metadata", {})),
            )
        )
    return store.get("last_refreshed_at"), rows
