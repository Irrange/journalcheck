from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from journalcheck.base import SiteChecker
from journalcheck.http import make_session
from journalcheck.models import SubmissionStatus
from journalcheck.utils import clean_text, is_active_status, parse_header_table


class EditorialManagerChecker(SiteChecker):
    site_name = "em"

    def __init__(self, base_url: str, journal_code: str, username: str, password: str, site_name: str = "em") -> None:
        self.base_url = base_url
        self.journal_code = journal_code
        self.username = username
        self.password = password
        self.site_name = site_name
        self.session = make_session()

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

        statuses: list[SubmissionStatus] = []
        for list_url in dict.fromkeys(list_urls):
            page = self.session.get(list_url, timeout=40)
            page.raise_for_status()
            soup = BeautifulSoup(page.text, "lxml")
            page_title = self._extract_page_title(soup)
            for row in parse_header_table(soup):
                manuscript_number = row.get("Manuscript Number", "")
                current_status = row.get("Current Status", "")
                if not manuscript_number or not is_active_status(current_status):
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
            raise RuntimeError(f"Editorial Manager login appears to have failed for {self.site_name}.")

    @staticmethod
    def _extract_page_title(soup: BeautifulSoup) -> str:
        title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        return title.split(" - ")[0] if " - " in title else title
