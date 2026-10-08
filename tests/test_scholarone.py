import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import sync_playwright

from journalcheck.server.account_urls import scholarone_url
from journalcheck.server.adapters import AuthenticationError, ChallengeError, ParseError, fetch_account
from journalcheck.server.scholarone import parse_author_page, read_scholarone
from journalcheck.server.store import Store
from journalcheck.server.worker import Worker, fetch_child
from tests.test_store import notification_config


URL = 'https://mc.manuscriptcentral.com/ageing'
FIXTURE = (Path(__file__).parent / 'fixtures/scholarone_queue.html').read_text()


@pytest.mark.parametrize('url,code', [(URL,'ageing'),(URL+'/?PARAMS=private-session#author','ageing'),
    ('https://mc.manuscriptcentral.com/t-its','t-its')])
def test_journal_code_comes_from_public_link_and_session_params_are_discarded(url, code):
    assert scholarone_url(url) == (f'https://mc.manuscriptcentral.com/{code}', code)


@pytest.mark.parametrize('url', ['http://mc.manuscriptcentral.com/ageing',
    'https://mc.manuscriptcentral.com/', 'https://evil.test/ageing',
    'https://mc.manuscriptcentral.com.evil.test/ageing',
    'https://mc.manuscriptcentral.com:1234/ageing',
    'https://secret@mc.manuscriptcentral.com/ageing',
    'https://mc.manuscriptcentral.com/ageing/other',
    'https://mc.manuscriptcentral.com/%2e%2e', None])
def test_non_official_or_ambiguous_links_are_rejected(url):
    with pytest.raises(ValueError):
        scholarone_url(url)


def test_queue_parser_keeps_all_articles_and_terminal_states_without_inventing_counts():
    rows = parse_author_page(FIXTURE, URL)
    assert [r['manuscript_number'] for r in rows] == ['AGE-2026-001','AGE-2026-002']
    assert rows[0]['status']=='Awaiting Reviewer Scores'
    assert rows[0]['title']=='A synthetic study of healthy ageing'
    assert rows[0]['submission_date']=='01-Oct-2026' and rows[0]['status_date'] is None
    assert rows[0]['metadata']['journal_name']=='Example Journal of Ageing'
    assert rows[0]['detail_url']==URL and rows[1]['status']=='Accepted'
    assert all(r['reviewer_invited'] is None and r['reviewer_accepted'] is None for r in rows)
    assert 'Synthetic Editor' not in rows[0]['status']


def test_responsive_cell_labels_and_explicit_empty_queue_are_supported():
    page='<table id="authorDashboardQueue"><tbody><tr id="queue_0"><td data-title="ID">ID: AGE-3</td><td data-title="Title">Sample title</td><td data-title="Status">Awaiting AE Assignment</td></tr></tbody></table>'
    rows=parse_author_page(page,URL)
    assert rows[0]['manuscript_number']=='AGE-3' and rows[0]['source']=='Age and Ageing'
    assert parse_author_page('<table id="authorDashboardQueue"><tbody><tr><td>You have no manuscripts in this queue.</td></tr></tbody></table>',URL)==[]


@pytest.mark.parametrize('page', ['<h1>Unknown page</h1>',
    '<table id="authorDashboardQueue"><tbody></tbody></table>',
    FIXTURE.replace('AGE-2026-001',''), FIXTURE.replace('Awaiting Reviewer Scores',''),
    FIXTURE.replace('<th>ID</th>','<th>Unrecognized column</th>')])
def test_changed_layout_or_incomplete_row_never_becomes_empty_success(page):
    with pytest.raises(ParseError):
        parse_author_page(page,URL)


def test_login_and_security_challenges_are_separate_from_parse_failures():
    with pytest.raises(AuthenticationError):
        parse_author_page('<form><input name="PASSWORD" type="password"></form>',URL)
    with pytest.raises(ChallengeError):
        parse_author_page('<title>Just a moment...</title><input name="cf-turnstile-response">',URL)


def test_decision_dates_and_terminal_history_are_preserved_without_title_actions(tmp_path):
    html=FIXTURE.replace('Accepted','Immediate Reject and Transfer to Example Journal (07-Jun-2026)')
    html=html.replace('A synthetic accepted article',
        'A synthetic accepted article<br><a>View Submission</a><br>Submitting Author: Synthetic Author<br><a>Cover Letter</a>')
    rows=parse_author_page(html,URL)
    assert rows[1]['status']=='Immediate Reject and Transfer to Example Journal'
    assert rows[1]['status_date']=='2026-06-07'
    assert rows[1]['title']=='A synthetic accepted article'
    store=Store(tmp_path)
    a=store.save_account({'name':'Synthetic ScholarOne','platform':'scholarone','base_url':URL,
        'username':'synthetic@example.test','password':'synthetic-password'})
    store.apply_rows(a,rows,store.settings(),verified=True)
    assert len(store.submissions())==1 and len(store.submissions(True))==1
    assert store.submissions(True)[0]['status_since_source']=='platform'
    assert store.submission(store.submissions(True)[0]['id'])['events']


def test_dispatch_and_worker_classify_security_challenge_without_logging_response(monkeypatch, capsys):
    account={'platform':'scholarone','base_url':URL,'username':'dummy','password':'synthetic-secret'}
    def blocked(*args):
        raise ChallengeError('upstream secret that must not be logged')
    monkeypatch.setattr('journalcheck.server.scholarone.fetch_scholarone',blocked)
    with pytest.raises(ChallengeError):
        fetch_account(account,{})
    import io
    monkeypatch.setattr('sys.stdin',io.StringIO(json.dumps({'account':account,'settings':{}})))
    fetch_child()
    result=json.loads(capsys.readouterr().out)
    assert result=={'ok':False,'category':'challenge'}


def test_challenge_preserves_last_success_and_alerts_once(tmp_path):
    store=Store(tmp_path)
    account=store.save_account({'name':'AGEING login','platform':'scholarone','base_url':URL,
        'journal_code':'wrong-user-code','username':'author@example.test','password':'synthetic-secret'})
    assert account['journal_code']=='ageing'
    cfg=notification_config();store.update_settings(cfg)
    store.apply_rows(account,parse_author_page(FIXTURE,URL),cfg,verified=True)
    previous=store.account(account['id'])['last_success_at']
    for _ in range(2):
        store.queue_job(account_id=account['id'])
        assert Worker(store,fetcher=lambda *a:{'ok':False,'category':'challenge'}).run_job()
    assert store.account(account['id'])['last_success_at']==previous
    assert store.account(account['id'])['error_category']=='challenge'
    assert store.submissions()[0]['status']=='Awaiting Reviewer Scores'
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]==1
        assert '尚未验证密码' in json.loads(db.execute('SELECT payload FROM notifications').fetchone()[0])['body']


@pytest.mark.parametrize('scenario', ['success','coauthor_only','challenge','bad_password','external_form','other_journal_form','other_port_form','broken_list'])
def test_offline_browser_login_navigation_pagination_and_failure_protection(scenario):
    # Ordinary browser logic against intercepted synthetic pages, never the journal.
    account={'base_url':URL,'username':'synthetic-user','password':'synthetic-browser-password'}
    nav='<nav><a href="?stage=live">Submitted Manuscripts</a><a href="?stage=decision">Manuscripts with Decisions</a><a href="?stage=coauthor">Manuscripts I Have Co-Authored</a></nav>'
    pages={
        'login':'<form action="?stage=home" method="post"><input name="USERID"><input type="password" name="PASSWORD"><button id="logInButton">Log In</button></form>',
        'home':'<a role="tab" href="?stage=author"><span>\uf040</span> Author</a>',
        'author':nav,
        'live':nav+FIXTURE+'<div class="pagination"><a href="?stage=live2">Next</a></div>',
        'live2':nav+FIXTURE.replace('AGE-2026-001','AGE-2026-003').replace('AGE-2026-002','AGE-2026-004'),
        'decision':nav+FIXTURE,
        'coauthor':nav+'<table id="authorDashboardQueue"><tbody><tr><td>No manuscripts found.</td></tr></tbody></table>',
    }
    if scenario=='challenge':
        pages['login']='<title>Just a moment...</title><input name="cf-turnstile-response">'
    if scenario=='bad_password':
        pages['home']=pages['login']+'<div>Invalid user ID or password</div>'
    if scenario=='external_form':
        pages['login']=pages['login'].replace('?stage=home','https://untrusted.test/login')
    if scenario=='other_journal_form':
        pages['login']=pages['login'].replace('?stage=home','https://mc.manuscriptcentral.com/another?stage=home')
    if scenario=='other_port_form':
        pages['login']=pages['login'].replace('?stage=home','https://mc.manuscriptcentral.com:444/ageing?stage=home')
    if scenario=='broken_list':
        pages['live']=nav+'<table id="authorDashboardQueue"><tbody></tbody></table>'
    if scenario=='coauthor_only':
        coauthor='<a role="tab" href="?stage=coauthor">2 Manuscripts I Have Co-Authored ›</a>'
        import re
        queue_only=re.sub(r'<nav>.*?</nav>', '', FIXTURE)
        pages['author']=coauthor+queue_only
        pages['coauthor']=coauthor+queue_only
    visited=[];submitted=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
        context=browser.new_context();page=context.new_page()
        def handle(route):
            url=urlsplit(route.request.url)
            assert url.hostname=='mc.manuscriptcentral.com'
            stage=parse_qs(url.query).get('stage',['login'])[0]
            visited.append(stage)
            assert stage in pages, 'Only author read-navigation is allowed'
            if route.request.method=='POST':
                submitted.append(parse_qs(route.request.post_data))
            route.fulfill(status=403 if scenario=='challenge' else 200,content_type='text/html',body=pages[stage])
        context.route('**/*',handle)
        try:
            if scenario=='coauthor_only':
                rows=read_scholarone(page,account)
                assert len(rows)==2 and 'live' not in visited and 'coauthor' in visited
                assert len(submitted)==1
            elif scenario=='success':
                rows=read_scholarone(page,account)
                assert len(rows)==4 and 'live2' in visited and 'decision' in visited and 'coauthor' in visited
                assert submitted==[{'USERID':[account['username']],'PASSWORD':[account['password']]}]
            else:
                error=ChallengeError if scenario=='challenge' else ParseError if scenario=='broken_list' else AuthenticationError
                with pytest.raises(error):
                    read_scholarone(page,account)
                if scenario in ('challenge','external_form','other_journal_form','other_port_form'):
                    assert not submitted
        finally:
            context.close();browser.close()


@pytest.mark.parametrize('stale_cookie', [False, True])
def test_automatic_login_refreshes_the_saved_session_after_author_center(stale_cookie):
    # A fresh profile only reaches the login form after the automatic POST;
    # the successful author-center arrival must trigger the session-save hook.
    account={'base_url':URL,'username':'synthetic-user','password':'synthetic-browser-password'}
    import re
    pages={
        'login':'<form action="?stage=home" method="post"><input name="USERID"><input type="password" name="PASSWORD"><button id="logInButton">Log In</button></form>',
        'home':re.sub(r'<nav>.*?</nav>','',FIXTURE),
    }
    submitted=[];saved=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
        context=browser.new_context();page=context.new_page()
        if stale_cookie:
            context.add_cookies([{'name':'session', 'value':'expired-on-server', 'url':URL}])
        def handle(route):
            url=urlsplit(route.request.url)
            assert url.hostname=='mc.manuscriptcentral.com'
            stage=parse_qs(url.query).get('stage',['login'])[0]
            if route.request.method=='POST':
                submitted.append(parse_qs(route.request.post_data))
            route.fulfill(status=200,content_type='text/html',body=pages[stage])
        context.route('**/*',handle)
        try:
            rows=read_scholarone(page,account,on_authenticated=lambda:saved.append(True))
            assert len(rows)==2 and submitted==[{'USERID':[account['username']],'PASSWORD':[account['password']]}]
            assert saved, 'session must be refreshed after automatic login'
        finally:
            context.close();browser.close()
