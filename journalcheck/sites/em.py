from __future__ import annotations

import re

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from journalcheck.base import SiteChecker
from journalcheck.http import make_session
from journalcheck.models import SubmissionStatus
from journalcheck.utils import clean_text, is_active_status, parse_header_table
from journalcheck.server.adapters import AuthenticationError, ParseError


class EditorialManagerChecker(SiteChecker):
    site_name = "em"

    def __init__(self, base_url: str, journal_code: str, username: str, password: str, site_name: str = "em", session=None, include_inactive: bool = False, strict: bool = False) -> None:
        self.base_url = base_url
        self.journal_code = journal_code
        self.username = username
        self.password = password
        self.site_name = site_name
        self.session = session if session is not None else make_session()
        self.include_inactive = include_inactive
        self.strict = strict

    def check(self) -> list[SubmissionStatus]:
        self._login()
        menu_url = f"https://www.editorialmanager.com/{self.journal_code}/AuthorMainMenu.aspx"
        menu_page = self.session.get(menu_url, timeout=40)
        menu_page.raise_for_status()
        menu_soup = BeautifulSoup(menu_page.text, "lxml")

        list_urls: list[str] = []
        for link in menu_soup.select("a[href]"):
            href = link.get("href", "")
            if href.startswith("auth_"):
                list_urls.append(urljoin(menu_url, href))
        if self.strict:
            menu_text = clean_text(menu_soup.get_text(" ", strip=True)).lower()
            author_markers = ("author main menu", "author center", "submissions being processed", "new submissions")
            if not list_urls and not any(marker in menu_text for marker in author_markers):
                raise ParseError("Editorial Manager author menu was not recognized.")
            if not list_urls and not self._has_explicit_empty_state(menu_soup):
                raise ParseError("Editorial Manager author menu has no recognized submission lists.")

        statuses: list[SubmissionStatus] = []
        for list_url in dict.fromkeys(list_urls):
            page = self.session.get(list_url, timeout=40)
            page.raise_for_status()
            soup = BeautifulSoup(page.text, "lxml")
            if self.strict:
                columns = [clean_text(tag.get_text(" ", strip=True)) for tag in soup.select("thead th")]
                if "Manuscript Number" not in columns or "Current Status" not in columns:
                    raise ParseError(f"Editorial Manager list {list_url} lacks required columns.")
                if not soup.select_one("tbody") and not self._has_explicit_empty_state(soup):
                    raise ParseError(f"Editorial Manager list {list_url} has no recognized body or empty state.")
            page_title = self._extract_page_title(soup)
            parsed_rows = parse_header_table(soup)
            if self.strict and self._has_explicit_empty_state(soup) and not any(row.get("Current Status") for row in parsed_rows):
                continue
            if self.strict and not parsed_rows:
                raise ParseError("Editorial Manager list lacked rows and an explicit empty-list marker.")
            for row in parsed_rows:
                manuscript_number = row.get("Manuscript Number", "")
                current_status = row.get("Current Status", "")
                if self.strict and (not manuscript_number or not current_status):
                    raise ParseError("Editorial Manager manuscript row is missing number or current status.")
                if not manuscript_number or (not self.include_inactive and not is_active_status(current_status)):
                    continue
                statuses.append(
                    SubmissionStatus(
                        site=self.site_name,
                        source=page_title,
                        manuscript_number=manuscript_number,
                        title=row.get("Title", ""),
                        status=current_status,
                        status_date=row.get("Status Date") or None,
                        submission_date=row.get("Initial Date Submitted") or None,
                        detail_url=list_url,
                        metadata={"journal_name": self._extract_journal_name(soup)},
                    )
                )
        return statuses

    def _login(self) -> None:
        login_page = self.session.get(self.base_url, timeout=40)
        login_page.raise_for_status()
        soup = BeautifulSoup(login_page.text, "lxml")
        payload = {
            tag.get("name"): tag.get("value", "")
            for tag in soup.select("input[name]")
            if tag.get("name")
        }
        payload["username"] = self.username
        payload["password"] = self.password
        payload["role"] = "author"

        action_url = f"https://www.editorialmanager.com/{self.journal_code}/LoginAction.ashx"
        response = self.session.post(action_url, data=payload, timeout=40)
        response.raise_for_status()
        if "AuthorMainMenu.aspx" not in response.text:
            if self.strict:
                raise AuthenticationError(f"Editorial Manager login could not be confirmed for {self.site_name}.")
            raise RuntimeError(f"Editorial Manager login appears to have failed for {self.site_name}.")

    @staticmethod
    def _extract_page_title(soup: BeautifulSoup) -> str:
        title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        return title.split(" - ")[0] if " - " in title else title

    @staticmethod
    def _extract_journal_name(soup: BeautifulSoup) -> str:
        tag = soup.select_one('meta[name="citation_journal_title"]')
        if tag and tag.get('content'):
            return clean_text(tag['content'])
        title = clean_text(soup.title.get_text(' ', strip=True) if soup.title else '')
        parts = re.split(r'\s+(?:-|\|)\s+', title)
        generic = re.compile(r'^(?:editorial manager[®™]?|submissions(?: .*?)?|author (?:main menu|center)|'
                             r'(?:new|live|revised|completed|post decision) (?:submissions|manuscripts)|'
                             r'main menu|login|log in|sign in)$', re.I)
        # Only a title explicitly paired with a known system label is evidence of a journal name.
        if len(parts) > 1 and any(generic.fullmatch(part) for part in parts):
            return next((part for part in reversed(parts) if part and not generic.fullmatch(part)), '')
        return ''

    @staticmethod
    def _has_explicit_empty_state(soup: BeautifulSoup) -> bool:
        text = clean_text(soup.get_text(" ", strip=True)).lower()
        return bool(re.search(r"\b(?:no|there are no|you have no)\s+(?:active\s+|live\s+|post decision\s+)?(?:manuscripts|submissions|records)\b", text))
