from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Mapping

from journalcheck.models import SubmissionStatus
from journalcheck.utils import submission_key, write_csv, write_json


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"records": []}
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _record_key(record: Mapping[str, Any]) -> str:
    key = submission_key(str(record.get("site", "")), str(record.get("manuscript_number", "")))
    if key:
        return key
    return str(record.get("key", ""))


def _record_to_row(record: Mapping[str, Any]) -> SubmissionStatus:
    notes = record.get("notes", [])
    review_comments = record.get("review_comments", [])
    metadata = record.get("metadata", {})
    return SubmissionStatus(
        site=str(record.get("site", "")),
        source=str(record.get("source", "")),
        manuscript_number=str(record.get("manuscript_number", "")),
        title=str(record.get("title", "")),
        status=str(record.get("status", "")),
        status_date=record.get("status_date"),
        submission_date=record.get("submission_date"),
        detail_url=record.get("detail_url"),
        reviewer_invited=record.get("reviewer_invited"),
        reviewer_accepted=record.get("reviewer_accepted"),
        review_reports_received=record.get("review_reports_received"),
        review_comments_url=record.get("review_comments_url"),
        review_comments=list(review_comments) if isinstance(review_comments, list) else [],
        notes=list(notes) if isinstance(notes, list) else [],
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def _flatten_rows(rows: list[SubmissionStatus]) -> list[dict[str, object]]:
    flattened: list[dict[str, object]] = []
    for row in rows:
        item = row.to_dict()
        item["notes"] = " | ".join(row.notes)
        item["review_comments"] = "\n\n".join(row.review_comments)
        item["metadata"] = item["metadata"] if isinstance(item["metadata"], str) else str(item["metadata"])
        flattened.append(item)
    return flattened


def _progress_summary(old: dict[str, Any], row: SubmissionStatus) -> str | None:
    changes: list[str] = []
    pairs = [
        ("reviewer_invited", "invited"),
        ("reviewer_accepted", "accepted"),
        ("review_reports_received", "reports"),
    ]
    for key, label in pairs:
        old_value = old.get(key)
        new_value = getattr(row, key)
        if old_value != new_value and (old_value is not None or new_value is not None):
            changes.append(f"{label}: {old_value} -> {new_value}")
    old_comment_count = len(old.get("review_comments", [])) if isinstance(old.get("review_comments"), list) else 0
    new_comment_count = len(row.review_comments)
    if old_comment_count != new_comment_count:
        changes.append(f"review comments: {old_comment_count} -> {new_comment_count}")
    return "; ".join(changes) if changes else None


def describe_change(change: dict[str, Any]) -> str:
    change_type = change.get("type")
    if change_type == "new_submission":
        return f"new active submission: {change.get('new_status')}"
    if change_type == "status_changed":
        return f"status changed: {change.get('old_status')} -> {change.get('new_status')}"
    if change_type == "progress_changed":
        return f"progress updated: {change.get('summary')}"
    if change_type == "no_longer_active":
        return f"no longer active; previous status: {change.get('old_status')}"
    return change_type or "unknown change"


def detect_changes(
    existing_store: dict[str, Any],
    rows: list[SubmissionStatus],
    inactive_rows: list[SubmissionStatus] | None = None,
) -> list[dict[str, Any]]:
    existing_records = {_record_key(record): record for record in existing_store.get("records", []) if _record_key(record)}
    current_rows = {row.key(): row for row in rows}
    inactive_row_map = {row.key(): row for row in (inactive_rows or [])}
    changes: list[dict[str, Any]] = []

    for key, row in current_rows.items():
        old = existing_records.get(key)
        if old is None:
            changes.append(
                {
                    "type": "new_submission",
                    "key": key,
                    "site": row.site,
                    "manuscript_number": row.manuscript_number,
                    "title": row.title,
                    "new_status": row.status,
                    "detail_url": row.detail_url,
                }
            )
            continue

        old_status = old.get("status")
        old_status_date = old.get("status_date")
        if old_status != row.status or old_status_date != row.status_date:
            changes.append(
                {
                    "type": "status_changed",
                    "key": key,
                    "site": row.site,
                    "manuscript_number": row.manuscript_number,
                    "title": row.title,
                    "old_status": old_status,
                    "new_status": row.status,
                    "old_status_date": old_status_date,
                    "new_status_date": row.status_date,
                    "detail_url": row.detail_url,
                }
            )
            continue

        summary = _progress_summary(old, row)
        if summary:
            changes.append(
                {
                    "type": "progress_changed",
                    "key": key,
                    "site": row.site,
                    "manuscript_number": row.manuscript_number,
                    "title": row.title,
                    "summary": summary,
                    "detail_url": row.detail_url,
                }
            )

    for key, row in inactive_row_map.items():
        if key in current_rows:
            continue
        old = existing_records.get(key)
        if old is None:
            continue
        old_status = old.get("status")
        old_status_date = old.get("status_date")
        if old_status != row.status or old_status_date != row.status_date:
            changes.append(
                {
                    "type": "status_changed",
                    "key": key,
                    "site": row.site,
                    "manuscript_number": row.manuscript_number,
                    "title": row.title,
                    "old_status": old_status,
                    "new_status": row.status,
                    "old_status_date": old_status_date,
                    "new_status_date": row.status_date,
                    "detail_url": row.detail_url,
                }
            )

    for key, old in existing_records.items():
        if key in current_rows or key in inactive_row_map:
            continue
        changes.append(
            {
                "type": "no_longer_active",
                "key": key,
                "site": old.get("site"),
                "manuscript_number": old.get("manuscript_number"),
                "title": old.get("title"),
                "old_status": old.get("status"),
                "detail_url": old.get("detail_url"),
            }
        )

    return changes


def append_refresh_log(log_path: Path, rows: list[SubmissionStatus], changes: list[dict[str, Any]], refreshed_at: str) -> None:
    fieldnames = [
        "refreshed_at",
        "event_type",
        "changed",
        "change_type",
        "change_detail",
        "active_count",
        "change_count",
        "site",
        "manuscript_number",
        "title",
        "status",
        "status_date",
        "submission_date",
        "source",
        "detail_url",
        "reviewer_invited",
        "reviewer_accepted",
        "review_reports_received",
    ]
    current_keys = {row.key() for row in rows}
    change_map = {change.get("key"): change for change in changes if change.get("key") in current_keys}
    need_header = not log_path.exists()
    with log_path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if need_header:
            writer.writeheader()
        writer.writerow(
            {
                "refreshed_at": refreshed_at,
                "event_type": "refresh_summary",
                "changed": "yes" if changes else "no",
                "change_type": "refresh_summary",
                "change_detail": f"{len(changes)} change(s); {len(rows)} active submission(s)",
                "active_count": len(rows),
                "change_count": len(changes),
                "site": "",
                "manuscript_number": "",
                "title": "",
                "status": "",
                "status_date": "",
                "submission_date": "",
                "source": "",
                "detail_url": "",
                "reviewer_invited": "",
                "reviewer_accepted": "",
                "review_reports_received": "",
            }
        )
        for row in rows:
            change = change_map.get(row.key())
            writer.writerow(
                {
                    "refreshed_at": refreshed_at,
                    "event_type": "snapshot",
                    "changed": "yes" if change else "no",
                    "change_type": change.get("type") if change else "",
                    "change_detail": describe_change(change) if change else "",
                    "active_count": "",
                    "change_count": "",
                    "site": row.site,
                    "manuscript_number": row.manuscript_number,
                    "title": row.title,
                    "status": row.status,
                    "status_date": row.status_date,
                    "submission_date": row.submission_date,
                    "source": row.source,
                    "detail_url": row.detail_url,
                    "reviewer_invited": row.reviewer_invited,
                    "reviewer_accepted": row.reviewer_accepted,
                    "review_reports_received": row.review_reports_received,
                }
            )
        for change in changes:
            if change.get("key") in current_keys:
                continue
            is_removal = change.get("type") == "no_longer_active"
            writer.writerow(
                {
                    "refreshed_at": refreshed_at,
                    "event_type": "removal" if is_removal else "transition",
                    "changed": "yes",
                    "change_type": change.get("type"),
                    "change_detail": describe_change(change),
                    "active_count": "",
                    "change_count": "",
                    "site": change.get("site"),
                    "manuscript_number": change.get("manuscript_number"),
                    "title": change.get("title"),
                    "status": change.get("old_status") if is_removal else change.get("new_status"),
                    "status_date": change.get("new_status_date") or change.get("old_status_date") or "",
                    "submission_date": "",
                    "source": "",
                    "detail_url": change.get("detail_url"),
                    "reviewer_invited": "",
                    "reviewer_accepted": "",
                    "review_reports_received": "",
                }
            )


def update_store(
    json_path: Path,
    csv_path: Path,
    log_path: Path,
    rows: list[SubmissionStatus],
    refreshed_at: str,
    inactive_rows: list[SubmissionStatus] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    existing_store = _load_existing(json_path)
    changes = detect_changes(existing_store, rows, inactive_rows=inactive_rows)
    existing_records = {_record_key(record): record for record in existing_store.get("records", []) if _record_key(record)}

    next_records: list[dict[str, Any]] = []
    for row in rows:
        base = row.to_dict()
        key = row.key()
        old = existing_records.get(key, {})
        record = {**old, **base}
        record["key"] = key
        record["first_seen_at"] = old.get("first_seen_at", refreshed_at)
        record["last_seen_at"] = refreshed_at
        record["refresh_count"] = int(old.get("refresh_count", 0)) + 1

        history = list(old.get("history", []))
        snapshot = {
            "refreshed_at": refreshed_at,
            "status": row.status,
            "status_date": row.status_date,
            "notes": row.notes,
            "reviewer_invited": row.reviewer_invited,
            "reviewer_accepted": row.reviewer_accepted,
            "review_reports_received": row.review_reports_received,
            "comment_count": len(row.review_comments),
        }
        last = history[-1] if history else None
        comparable_keys = [
            "status",
            "status_date",
            "notes",
            "reviewer_invited",
            "reviewer_accepted",
            "review_reports_received",
            "comment_count",
        ]
        if last is None or any(last.get(name) != snapshot.get(name) for name in comparable_keys):
            history.append(snapshot)
        else:
            last["refreshed_at"] = refreshed_at
        record["history"] = history
        next_records.append(record)

    next_records.sort(key=lambda item: (item.get("site", ""), item.get("manuscript_number", "")))
    store = {
        "last_refreshed_at": refreshed_at,
        "record_count": len(next_records),
        "records": next_records,
    }
    write_json(json_path, store)
    write_csv(csv_path, _flatten_rows(rows))
    append_refresh_log(log_path, rows, changes, refreshed_at)
    return store, changes


def relabel_saved_snapshot(json_path: Path, csv_path: Path, site_map: Mapping[str, str]) -> None:
    if not site_map or not json_path.exists():
        return

    store = _load_existing(json_path)
    records = []
    changed = False
    for raw_record in store.get("records", []):
        record = dict(raw_record)
        old_site = str(record.get("site", ""))
        new_site = site_map.get(old_site)
        if new_site:
            record["site"] = new_site
            record["key"] = submission_key(new_site, str(record.get("manuscript_number", "")))
            changed = True
        records.append(record)

    if not changed:
        return

    store["records"] = records
    store["record_count"] = len(records)
    write_json(json_path, store)
    write_csv(csv_path, _flatten_rows([_record_to_row(record) for record in records]))
