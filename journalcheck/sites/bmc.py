from __future__ import annotations

import re

from bs4 import BeautifulSoup

from journalcheck.base import SiteChecker
from journalcheck.http import make_session
from journalcheck.models import SubmissionStatus
from journalcheck.utils import clean_text, is_active_status
from journalcheck.server.adapters import AuthenticationError, ParseError


class BMCChecker(SiteChecker):
    site_name = "bmc"

    def __init__(self, submission_url: str, username: str, password: str, site_name: str = "bmc", session=None, include_inactive: bool = False, strict: bool = False) -> None:
        self.submission_url = submission_url
        self.username = username
        self.password = password
        self.site_name = site_name
        self.session = session if session is not None else make_session()
        self.include_inactive = include_inactive
        self.strict = strict

    def check(self) -> list[SubmissionStatus]:
        final_page = self._login()
        return self.parse_page(final_page)

    def parse_page(self, final_page) -> list[SubmissionStatus]:
        soup = BeautifulSoup(final_page.text, "lxml")

        title = self._extract_submission_title(soup)
        status_node = soup.select_one("[data-current-step-description]")
        status = clean_text(status_node.get_text(" ", strip=True) if status_node else "")
        empty_node = soup.select_one("[data-test='no-submissions']")
        if self.strict and empty_node and "no submission" in clean_text(empty_node.get_text(" ", strip=True)).lower():
            return []
        submission_id = self._extract_submission_id(final_page.url, soup)
        if self.strict and (not status_node or not status):
            raise ParseError("BMC submission status selector was not recognized.")
        if self.strict and not submission_id:
            raise ParseError("BMC submission ID was not recognized.")
        if not self.include_inactive and not is_active_status(status):
            return []

        notes = [
            clean_text(tag.get_text(" ", strip=True))
            for tag in soup.select("[data-test='current-status-text']")
        ]
        news = [
            clean_text(tag.get_text(" ", strip=True))
            for tag in soup.select("[data-test='current-status-information-box-text']")
        ]
        submission_files = [
            clean_text(tag.get_text(" ", strip=True))
            for tag in soup.select("[data-test='your-submission-manuscript-file']")
        ]
        journal = self._extract_journal(soup)
        reviewer_invited, reviewer_accepted, review_reports_received, reviewer_display = self._extract_reviewer_counts(news, notes)

        metadata: dict[str, object] = {}
        if submission_files:
            metadata["files"] = submission_files
        if news:
            metadata["peer_review_updates"] = news
        metadata.update(reviewer_display)

        return [
            SubmissionStatus(
                site=self.site_name,
                source=journal,
                manuscript_number=submission_id,
                title=title,
                status=status,
                detail_url=final_page.url,
                reviewer_invited=reviewer_invited,
                reviewer_accepted=reviewer_accepted,
                review_reports_received=review_reports_received,
                notes=notes,
                metadata=metadata,
            )
        ]

    def _login(self):
        first_page = self.session.get(self.submission_url, timeout=40, allow_redirects=True)
        first_page.raise_for_status()
        first_soup = BeautifulSoup(first_page.text, "lxml")
        if first_soup.select_one('[data-current-step-description]'):
            return first_page

        first_form = first_soup.find("form")
        if first_form is None:
            if self.strict:
                raise ParseError("BMC login email form was not recognized.")
            raise RuntimeError("BMC login email form was not found.")
        first_action = "https://idp-personal-authenticator.springernature.com" + first_form.get("action", "")
        first_payload = {
            tag.get("name"): tag.get("value", "")
            for tag in first_form.select("input[name]")
            if tag.get("name")
        }
        first_payload["user_id"] = self.username

        second_page = self.session.post(first_action, data=first_payload, timeout=40, allow_redirects=True)
        second_page.raise_for_status()
        second_soup = BeautifulSoup(second_page.text, "lxml")

        second_form = second_soup.find("form")
        if second_form is None:
            if self.strict:
                raise AuthenticationError("BMC password form was not reached; login could not be confirmed.")
            raise RuntimeError("BMC login password form was not found.")
        second_action = "https://idp-personal-authenticator.springernature.com" + second_form.get("action", "")
        second_payload = {
            tag.get("name"): tag.get("value", "")
            for tag in second_form.select("input[name]")
            if tag.get("name")
        }
        second_payload["passwd"] = self.password

        final_page = self.session.post(second_action, data=second_payload, timeout=40, allow_redirects=True)
        final_page.raise_for_status()
        if "Submission Details" not in final_page.text:
            if self.strict:
                raise AuthenticationError(f"BMC login could not be confirmed for {self.site_name}.")
            raise RuntimeError(f"BMC login appears to have failed for {self.site_name}.")
        return final_page

    @staticmethod
    def _extract_submission_title(soup: BeautifulSoup) -> str:
        ignored = {
            "Current status",
            "News about your peer review process",
            "Your submission",
            "Submission history",
            "Learn about our submission process",
            "Progress so far",
            "Need help?",
        }
        for tag_name in ("h2", "p"):
            for tag in soup.select(tag_name):
                text = clean_text(tag.get_text(" ", strip=True))
                if not text or text in ignored:
                    continue
                if text.startswith("This is a new page"):
                    continue
                if len(text) > 40:
                    return text
        return ""

    @staticmethod
    def _extract_journal(soup: BeautifulSoup) -> str:
        node = soup.select_one("[data-test='page-title']")
        title = clean_text(node.get_text(" ", strip=True) if node else '')
        if ":" in title:
            return clean_text(title.split(":", 1)[1])
        return title

    @staticmethod
    def _extract_submission_id(url: str, soup: BeautifulSoup) -> str:
        match = re.search(r"/submission-details/([0-9a-f-]+)", url, re.I)
        if match:
            return match.group(1)
        context = soup.select_one("[data-test='sn-context']")
        text = clean_text(context.get_text(" ", strip=True) if context else "")
        match = re.search(r'submissionId = "([^"]+)"', text)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_reviewer_counts(news: list[str], notes: list[str]) -> tuple[int | None, int | None, int | None, dict[str, str]]:
        invited = None
        accepted = None
        received = None
        display: dict[str, str] = {}
        lines = [line for line in [*news, *notes] if line]
        for line in lines:
            if invited is None:
                match = re.search(r"(?:has )?invited\s+more than\s+(\d+)\s+reviewer\(s\)", line, re.I)
                if not match:
                    match = re.search(r"more than\s+(\d+)\s+reviewer\(s\)", line, re.I)
                if match:
                    invited = int(match.group(1))
                    display["reviewer_invited_display"] = f"{match.group(1)}+"
                else:
                    match = re.search(r"(?:has )?invited\s+(\d+)\s+reviewer", line, re.I)
                    if match:
                        invited = int(match.group(1))
            if accepted is None:
                match = re.search(r"(?:there are|has)\s+(\d+)\s+reviewer\(s\)\s+(?:that )?have accepted", line, re.I)
                if match:
                    accepted = int(match.group(1))
            if received is None:
                match = re.search(r"received\s+(\d+)\s+reviewer report", line, re.I)
                if match:
                    received = int(match.group(1))
        return invited, accepted, received, display
