from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from journalcheck.utils import submission_key


@dataclass(slots=True)
class SubmissionStatus:
    site: str
    source: str
    manuscript_number: str
    title: str
    status: str
    status_date: str | None = None
    submission_date: str | None = None
    detail_url: str | None = None
    reviewer_invited: int | None = None
    reviewer_accepted: int | None = None
    review_reports_received: int | None = None
    review_comments_url: str | None = None
    review_comments: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return submission_key(self.site, self.manuscript_number)

    def reviewer_value_display(self, field_name: str) -> str:
        display_key = f"{field_name}_display"
        display_value = self.metadata.get(display_key)
        if display_value not in (None, ""):
            return str(display_value)
        value = getattr(self, field_name)
        return str(value) if value is not None else "-"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
