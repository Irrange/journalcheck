from __future__ import annotations

import re

from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunparse

from bs4 import BeautifulSoup

from journalcheck.base import SiteChecker
from journalcheck.http import make_session
from journalcheck.models import SubmissionStatus
from journalcheck.utils import clean_text, parse_key_value_rows
from journalcheck.server.adapters import AuthenticationError, ParseError


class AHAChecker(SiteChecker):
    site_name = "aha"

    def __init__(self, base_url: str, username: str, password: str, site_name: str = "aha", session=None, include_inactive: bool = False, strict: bool = False) -> None:
        self.base_url = base_url
        parts = urlsplit(base_url)
        self.root_url = f"{parts.scheme}://{parts.netloc}/"
        self.username = username
        self.password = password
        self.site_name = site_name
        self.session = session if session is not None else make_session()
        self.include_inactive = include_inactive
        self.strict = strict

    def check(self) -> list[SubmissionStatus]:
        home = self._login()
        home_soup = BeautifulSoup(home.text, "lxml")
        folder_urls = self._find_folder_urls(home_soup)
        if not folder_urls:
            if self.strict:
                raise ParseError("AHA manuscript folders were not recognized.")
            return []

        statuses_by_key: dict[str, SubmissionStatus] = {}
        ordered_keys: list[str] = []
        for folder_name, folder_url in folder_urls.items():
            folder = self.session.get(folder_url, timeout=40)
            folder.raise_for_status()
            folder_soup = BeautifulSoup(folder.text, "lxml")
            detail_links = [
                urljoin(self.root_url, link["href"])
                for link in folder_soup.select("a[href]")
                if clean_text(link.get_text(" ", strip=True)).startswith("View Manuscript #")
            ]
            if self.strict and not detail_links and not self._has_explicit_empty_state(folder_soup):
                raise ParseError(f"AHA {folder_name} page has no manuscript links and no explicit empty state.")

            for detail_url in detail_links:
                status = self._parse_manuscript(detail_url, folder_name)
                if status is None:
                    if self.strict:
                        raise ParseError("AHA manuscript detail lacked both manuscript number and title.")
                    continue
                if self.strict and (not status.manuscript_number or not status.status):
                    raise ParseError("AHA manuscript detail is missing manuscript number or status.")
                dedupe_key = status.key() if status.manuscript_number else detail_url
                if dedupe_key not in statuses_by_key:
                    statuses_by_key[dedupe_key] = status
                    ordered_keys.append(dedupe_key)
                    continue
                if self._manuscript_priority(status) > self._manuscript_priority(statuses_by_key[dedupe_key]):
                    statuses_by_key[dedupe_key] = status
        return [statuses_by_key[key] for key in ordered_keys]

    def _login(self):
        login_page = self.session.get(self.base_url, timeout=40)
        login_page.raise_for_status()
        soup = BeautifulSoup(login_page.text, "lxml")
        payload = {
            tag.get("name"): tag.get("value", "")
            for tag in soup.select("input[name]")
            if tag.get("name") in {"form_type", "j_id", "ms_id_key"}
        }
        payload["login"] = self.username
        payload["password"] = self.password
        response = self.session.post(self.base_url, data=payload, timeout=40)
        response.raise_for_status()
        response_soup = BeautifulSoup(response.text, "lxml")
        if not self._find_folder_urls(response_soup):
            if self.strict:
                raise AuthenticationError(f"AHA login could not be confirmed for {self.site_name}.")
            raise RuntimeError(f"AHA login appears to have failed for {self.site_name}.")
        return response

    @staticmethod
    def _has_explicit_empty_state(soup: BeautifulSoup) -> bool:
        text = clean_text(soup.get_text(" ", strip=True)).lower()
        return bool(re.search(r"\b(?:no|there are no|you have no)\s+(?:active\s+|live\s+|post decision\s+)?(?:manuscripts|submissions|records)\b", text))

    def _find_folder_urls(self, soup: BeautifulSoup) -> dict[str, str]:
        folders: dict[str, str] = {}
        for link in soup.select("a[href]"):
            label = clean_text(link.get_text(" ", strip=True))
            if label.startswith("Live Manuscripts"):
                folders.setdefault("Live Manuscripts", urljoin(self.root_url, link["href"]))
            elif label.startswith("Post Decision Manuscripts"):
                folders.setdefault("Post Decision Manuscripts", urljoin(self.root_url, link["href"]))
        return folders

    def _manuscript_priority(self, row: SubmissionStatus) -> int:
        folder = str(row.metadata.get("folder", ""))
        return 1 if folder == "Live Manuscripts" else 0

    def _parse_manuscript(self, detail_url: str, folder_name: str) -> SubmissionStatus | None:
        detail_page = self.session.get(detail_url, timeout=40)
        detail_page.raise_for_status()
        soup = BeautifulSoup(detail_page.text, "lxml")
        fields = parse_key_value_rows(soup)

        status_soup = soup
        status_url = self._find_status_details_url(soup)
        if status_url is not None:
            status_page = self.session.get(status_url, timeout=40)
            status_page.raise_for_status()
            status_soup = BeautifulSoup(status_page.text, "lxml")

        status_fields = parse_key_value_rows(status_soup)
        status_history = self._parse_status_history(status_soup)
        current_status = status_history[0]["stage"] if status_history else status_fields.get("Current Stage", "")
        if not current_status:
            current_status = fields.get("Current Stage", "")
        if not current_status and folder_name == "Post Decision Manuscripts":
            current_status = "Decision"

        manuscript_number = fields.get("Manuscript #", "")
        title = fields.get("Title", "")
        if not manuscript_number and not title:
            return None

        status_date = status_history[0]["start_date"] if status_history else None
        review_comments_url = self._build_past_reviews_url(status_url) if status_url else None
        review_comments = self._fetch_review_comments(review_comments_url) if review_comments_url else []

        metadata: dict[str, object] = {"folder": folder_name}
        if status_history:
            metadata["stage_history"] = status_history

        return SubmissionStatus(
            site=self.site_name,
            source=folder_name,
            manuscript_number=manuscript_number,
            title=title,
            status=current_status,
            status_date=status_date,
            submission_date=fields.get("Submission Date") or None,
            detail_url=detail_url,
            review_comments_url=review_comments_url,
            review_comments=review_comments,
            metadata=metadata,
        )

    def _find_status_details_url(self, soup: BeautifulSoup) -> str | None:
        for link in soup.select("a[href]"):
            if clean_text(link.get_text(" ", strip=True)) == "Current Stage":
                return urljoin(self.root_url, link["href"])
        return None

    def _parse_status_history(self, soup: BeautifulSoup) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        found_stage_header = False
        for row in soup.select("tr"):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.select("th,td")]
            if cells[:2] == ["Stage", "Start Date"]:
                found_stage_header = True
                continue
            if found_stage_header and len(cells) >= 2 and cells[0]:
                rows.append({"stage": cells[0], "start_date": cells[1]})
        return rows

    def _build_past_reviews_url(self, status_url: str) -> str:
        parsed = urlparse(status_url)
        query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key == "ms_id_key":
                continue
            if key == "form_type":
                query.append((key, "past_reviews_reviewer_comments_display"))
            else:
                query.append((key, value))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))

    def _fetch_review_comments(self, comments_url: str) -> list[str]:
        page = self.session.get(comments_url, timeout=40)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "lxml")
        for tag in soup.select("script, style"):
            tag.decompose()

        ignored_exact = {
            "Journal of the American Heart Association",
            "Tracking System Home",
            "Author Help",
            "Reviewer Help",
            "Tips",
            "Change Journal",
            "Logout",
            "Past Reviews",
            "Journal Home",
            "Instructions for Authors",
            "Editorial Board",
            "Contact the Journal",
            "AHA Journals Home",
            "Feedback",
            "AHA Councils",
            "About AHA",
            "AHA Ethics Policy",
            "AHA Privacy Policy",
            "Donate to AHA",
            "Terms of Service",
            "EJPress Software by eJournalPress",
            "Licensed under",
            "?",
        }
        ignored_prefixes = (
            "Go to ",
            "American Heart Association",
            "Patent #",
            "Unauthorized use prohibited",
        )

        ignored_fragments = (
            "Tracking System Home",
            "Change Journal",
            "AHA Journal Portal",
            "EJPress Software by eJournalPress",
            "Terms of Service",
            "AHA Privacy Policy",
        )

        comments: list[str] = []
        selectors = "main p, main pre, main td, main li, body p, body pre, body td, body li"
        for tag in soup.select(selectors):
            text = clean_text(tag.get_text(" ", strip=True))
            if not text or text in ignored_exact:
                continue
            if any(text.startswith(prefix) for prefix in ignored_prefixes):
                continue
            if any(fragment in text for fragment in ignored_fragments):
                continue
            if len(text) < 40:
                continue
            if text not in comments:
                comments.append(text)
        return comments
