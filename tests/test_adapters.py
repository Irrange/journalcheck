from pathlib import Path

import pytest

from journalcheck.server.adapters import AuthenticationError, ParseError, fetch_account
from journalcheck.sites.aha import AHAChecker
from journalcheck.sites.bmc import BMCChecker
from journalcheck.sites.em import EditorialManagerChecker


FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, body: str, url: str):
        self.text = body
        self.url = url

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._response("GET", url)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._response("POST", url)

    def close(self):
        pass

    def _response(self, method, url):
        key = (method, url)
        if key not in self.routes:
            raise AssertionError(f"Unexpected fake request: {key}")
        body, final_url = self.routes[key]
        return FakeResponse(body, final_url)


def html(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def aha_session(folder="aha_folder.html", login="aha_home.html"):
    return FakeSession({
        ("GET", "https://aha.test/login"): (html("aha_login.html"), "https://aha.test/login"),
        ("POST", "https://aha.test/login"): (html(login), "https://aha.test/home"),
        ("GET", "https://aha.test/folder/live"): (html(folder), "https://aha.test/folder/live"),
        ("GET", "https://aha.test/folder/post"): (html(folder), "https://aha.test/folder/post"),
        ("GET", "https://aha.test/detail/123"): (html("aha_detail.html"), "https://aha.test/detail/123"),
    })


def em_session(list_page="em_list.html", menu="em_home.html", login_ok=True):
    response = html("em_home.html") if login_ok else "<html><body>Invalid credentials</body></html>"
    routes = {
        ("GET", "https://em.test/login"): (html("em_login.html"), "https://em.test/login"),
        ("POST", "https://www.editorialmanager.com/demo/LoginAction.ashx"): (response, "https://em.test/login-result"),
        ("GET", "https://www.editorialmanager.com/demo/AuthorMainMenu.aspx"): (html(menu), "https://www.editorialmanager.com/demo/AuthorMainMenu.aspx"),
        ("GET", "https://www.editorialmanager.com/demo/auth_list.aspx"): (html(list_page), "https://www.editorialmanager.com/demo/auth_list.aspx"),
    }
    return FakeSession(routes)


def bmc_session(detail="bmc_detail.html"):
    return FakeSession({
        ("GET", "https://bmc.test/start"): (html("bmc_login_email.html"), "https://bmc.test/start"),
        ("POST", "https://idp-personal-authenticator.springernature.com/email"): (html("bmc_login_password.html"), "https://idp.test/password"),
        ("POST", "https://idp-personal-authenticator.springernature.com/password"): (html(detail), "https://bmc.test/submission-details/123e4567-e89b-12d3-a456-426614174000"),
    })


def test_aha_returns_terminal_rows_and_accepts_explicit_empty_state():
    rows = AHAChecker("https://aha.test/login", "u", "p", session=aha_session(), strict=True, include_inactive=True).check()
    assert len(rows) == 1
    assert rows[0].status == "Rejected"
    assert rows[0].manuscript_number == "JHA-2026-000123"

    empty = AHAChecker("https://aha.test/login", "u", "p", session=aha_session("aha_empty.html"), strict=True).check()
    assert empty == []


def test_aha_rejects_unrecognized_folder_and_unconfirmed_login():
    with pytest.raises(ParseError):
        AHAChecker("https://aha.test/login", "u", "p", session=aha_session("aha_malformed.html"), strict=True).check()
    with pytest.raises(AuthenticationError):
        AHAChecker("https://aha.test/login", "u", "p", session=aha_session(login="aha_malformed.html"), strict=True).check()


def test_em_returns_terminal_rows_and_accepts_explicit_empty_menu():
    rows = EditorialManagerChecker("https://em.test/login", "demo", "u", "p", session=em_session(), strict=True, include_inactive=True).check()
    assert len(rows) == 1
    assert rows[0].status == "Rejected"
    assert rows[0].manuscript_number == "EM-26-00123"

    empty = EditorialManagerChecker("https://em.test/login", "demo", "u", "p", session=em_session(menu="em_empty.html"), strict=True).check()
    assert empty == []


def test_em_rejects_bad_columns_and_unconfirmed_login():
    with pytest.raises(ParseError):
        EditorialManagerChecker("https://em.test/login", "demo", "u", "p", session=em_session(list_page="em_malformed.html"), strict=True).check()
    with pytest.raises(AuthenticationError):
        EditorialManagerChecker("https://em.test/login", "demo", "u", "p", session=em_session(login_ok=False), strict=True).check()


def test_bmc_returns_terminal_row_and_recognizes_explicit_empty_state():
    rows = BMCChecker("https://bmc.test/start", "u", "p", session=bmc_session(), strict=True, include_inactive=True).check()
    assert len(rows) == 1
    assert rows[0].status == "Rejected"
    assert rows[0].manuscript_number == "123e4567-e89b-12d3-a456-426614174000"

    assert BMCChecker("https://bmc.test/start", "u", "p", session=bmc_session("bmc_empty.html"), strict=True).check() == []


def test_bmc_rejects_changed_structure_and_unconfirmed_login():
    with pytest.raises(ParseError):
        BMCChecker("https://bmc.test/start", "u", "p", session=bmc_session("bmc_malformed.html"), strict=True).check()
    with pytest.raises(AuthenticationError):
        BMCChecker("https://bmc.test/start", "u", "p", session=FakeSession({
            ("GET", "https://bmc.test/start"): (html("bmc_login_email.html"), "https://bmc.test/start"),
            ("POST", "https://idp-personal-authenticator.springernature.com/email"): (html("bmc_malformed.html"), "https://idp.test/password"),
        }), strict=True).check()


def test_fetch_account_dispatches_and_serializes_without_real_network(monkeypatch):
    session = aha_session()
    monkeypatch.setattr("journalcheck.server.adapters._AdapterSession", lambda network: session)
    rows = fetch_account({
        "platform": "aha", "name": "AHA demo", "id": "a1", "username": "u", "password": "p",
        "base_url": "https://aha.test/login", "journal_code": "", "submission_url": "",
    }, {"http_proxy": "http://proxy.invalid:8080", "https_proxy": "https://proxy.invalid:8080", "no_proxy": "localhost"})
    assert len(rows) == 1
    assert rows[0]["site"] == "AHA demo"
    assert rows[0]["status"] == "Rejected"


@pytest.mark.parametrize('platform',['aha','em','bmc'])
def test_three_platforms_retain_normal_active_status(platform):
    sessions={'aha':aha_session(),'em':em_session(),'bmc':bmc_session()}
    session=sessions[platform]
    session.routes={k:(body.replace('Rejected','Under Review'),url) for k,(body,url) in session.routes.items()}
    if platform=='aha':
        checker=AHAChecker('https://aha.test/login','u','p',session=session,strict=True,include_inactive=True)
    elif platform=='em':
        checker=EditorialManagerChecker('https://em.test/login','demo','u','p',session=session,strict=True,include_inactive=True)
    else:
        checker=BMCChecker('https://bmc.test/start','u','p',session=session,strict=True,include_inactive=True)
    assert checker.check()[0].status=='Under Review'


def test_unrelated_empty_message_is_not_an_empty_manuscript_list():
    session=aha_session()
    session.routes[('GET','https://aha.test/folder/live')]=('<p>There are no notifications today</p>','https://aha.test/folder/live')
    with pytest.raises(ParseError):
        AHAChecker('https://aha.test/login','u','p',session=session,strict=True).check()


def test_em_journal_name_is_distinct_from_submission_folder():
    from bs4 import BeautifulSoup
    from journalcheck.sites.em import EditorialManagerChecker
    page = BeautifulSoup(html('em_list.html'), 'lxml')
    assert EditorialManagerChecker._extract_page_title(page) == 'Submissions'
    assert EditorialManagerChecker._extract_journal_name(page) == 'Test Journal'
    unknown = BeautifulSoup('<title>Submissions Being Processed</title>', 'lxml')
    assert EditorialManagerChecker._extract_journal_name(unknown) == ''
