from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup, Tag

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional fallback
    load_dotenv = None


def load_env(root: Path) -> None:
    env_path = root / ".env"
    if load_dotenv is not None and env_path.exists():
        load_dotenv(env_path, override=False)


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def env_optional(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def clean_text(value: str | None) -> str:
    if value is None:
        return ""
    value = value.replace("\xa0", " ").replace("\u2003", " ")
    return re.sub(r"\s+", " ", value).strip()


def site_family(site_name: str | None) -> str:
    normalized = clean_text(site_name).lower()
    if not normalized:
        return ""
    return normalized.split("_", 1)[0]


def submission_key(site_name: str | None, manuscript_number: str | None) -> str:
    normalized_site = clean_text(site_name).lower()
    normalized_manuscript = clean_text(manuscript_number)
    if not normalized_site:
        return normalized_manuscript
    if site_family(normalized_site) == "bmc" and normalized_manuscript:
        return f"bmc:{normalized_manuscript}"
    return f"{normalized_site}:{normalized_manuscript}"


def parse_key_value_rows(soup: BeautifulSoup | Tag) -> dict[str, str]:
    rows: dict[str, str] = {}
    for row in soup.select("tr"):
        headers = row.select("th")
        cells = row.select("td")
        if len(headers) == 1 and len(cells) >= 1:
            key = clean_text(headers[0].get_text(" ", strip=True))
            value = clean_text(cells[0].get_text(" ", strip=True))
            if key:
                rows[key] = value
    return rows


def parse_header_table(soup: BeautifulSoup | Tag) -> list[dict[str, str]]:
    header_cells = [clean_text(tag.get_text(" ", strip=True)) for tag in soup.select("thead th")]
    if not header_cells:
        return []
    records: list[dict[str, str]] = []
    for row in soup.select("tbody tr"):
        values = [clean_text(tag.get_text(" ", strip=True)) for tag in row.select("td")]
        if not values:
            continue
        data = dict(zip(header_cells, values))
        if any(data.values()):
            records.append(data)
    return records


def ensure_output_dir(root: Path, output_dir: str) -> Path:
    path = root / output_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: Iterable[dict[str, object]]) -> None:
    import csv

    rows = list(rows)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def is_active_status(status: str | None) -> bool:
    normalized = clean_text(status).lower()
    if not normalized:
        return True
    inactive_fragments = (
        "accepted",
        "rejected",
        "withdrawn",
        "with a decision",
        "production completed",
        "declined",
    )
    if normalized in {"decision", "completed"}:
        return False
    return not any(fragment in normalized for fragment in inactive_fragments)
