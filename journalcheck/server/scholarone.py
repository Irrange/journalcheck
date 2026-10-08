"""Automatic official ScholarOne login and read-only queues, with session reuse."""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from journalcheck.models import SubmissionStatus
from journalcheck.utils import clean_text
from .account_urls import scholarone_url
from .adapters import AuthenticationError, ChallengeError, ParseError
from .journal_names import page_journal_name


QUEUES = ('Submitted Manuscripts', 'Manuscripts Awaiting Revision',
          'Manuscripts in Revision', 'Manuscripts Awaiting Resubmission',
          'Manuscripts with Decisions', 'Manuscripts I Have Co-Authored')
HEADERS = {
    'id':'manuscript_number', 'manuscript id':'manuscript_number',
    'manuscript number':'manuscript_number', 'submission id':'manuscript_number',
    'title':'title', 'manuscript title':'title',
    'status':'status', 'current status':'status', 'manuscript status':'status',
    'submitted':'submission_date', 'date submitted':'submission_date',
    'submitted on':'submission_date', 'submission date':'submission_date',
    'status date':'status_date',
}
EMPTY = re.compile(r'\b(?:no manuscripts(?:\s+(?:found|submitted|to display|in this queue))?'
                   r'|you (?:currently )?have no (?:submitted )?manuscripts'
                   r'|there are no manuscripts(?:\s+in this (?:list|queue))?)\b', re.I)


def _header(value):
    return re.sub(r'\s+', ' ', clean_text(value).lower()).strip(' :')


def _challenge(soup):
    title = clean_text(soup.title.get_text()) if soup.title else ''
    body = clean_text(soup.get_text(' ', strip=True)).lower()
    return (title.lower() in ('just a moment...', 'attention required! | cloudflare')
            or 'performing security verification' in body
            or 'verify you are human' in body
            or soup.select_one('input[name="cf-turnstile-response"], #challenge-form, .cf-turnstile, iframe[src*="challenges.cloudflare.com"]'))


def check_page(html):
    soup = BeautifulSoup(html, 'lxml')
    if _challenge(soup):
        raise ChallengeError('ScholarOne 要求安全验证，尚未验证账号密码。')
    if not soup.select_one('#authorDashboardQueue') and soup.select_one('input[name="PASSWORD"], input[type="password"]'):
        raise AuthenticationError('ScholarOne 登录未完成，请检查官方登录页。')
    return soup


def _cell_text(cell, field):
    if field == 'title':
        title = cell.select_one('.manuscript-title, .submission-title')
        if title:
            return clean_text(title.get_text(' ', strip=True))
        # Official TITLE cells also contain View Submission, Cover Letter and
        # Submitting Author. Their text is not part of the manuscript title.
        candidates = [clean_text(line) for line in cell.get_text('\n', strip=True).splitlines()]
        return next((line for line in candidates if line and not re.match(
            r'^(?:View Submission|View Decision Letter|Cover Letter|Submitting Author\s*:)', line, re.I)), '')
    if field == 'status':
        candidates = cell.select('.pagecontents, .manuscript-status')
        parts = [clean_text(tag.get_text(' ', strip=True)) for tag in candidates]
        parts += [clean_text(line) for line in cell.get_text('\n', strip=True).splitlines()]
        return next((p for p in parts if p and not re.match(r'^(?:ADM|EIC|AE|INF|GE):', p, re.I)
                     and p.lower() not in ('contact journal', 'view submission', 'view decision letter')), '')
    return clean_text(cell.get_text(' ', strip=True))


def _status_date(data):
    # Decision rows put a real date after the status, e.g. Reject (06-Jul-2025).
    # Do not treat review-round numbers or arbitrary parentheses as dates.
    match = re.search(r'\s*\(([^()]+)\)\s*$', data['status'])
    if not match:
        return
    supplied = match.group(1).strip()
    for format in ('%d-%b-%Y', '%d %b %Y', '%d %B %Y', '%b %d, %Y', '%Y-%m-%d'):
        try:
            parsed = datetime.strptime(supplied, format).date().isoformat()
        except ValueError:
            continue
        status = data['status'][:match.start()].strip()
        if not status:
            return
        data['status'] = status
        data['status_date'] = data.get('status_date') or parsed
        return


def parse_author_page(html, base_url):
    """Accept recognized queues only; malformed rows never mean an empty list."""
    base_url, code = scholarone_url(base_url)
    soup = check_page(html)
    container = soup.select_one('#authorDashboardQueue')
    if container is None:
        raise ParseError('ScholarOne 作者稿件列表未识别。')
    table = container if container.name == 'table' else container.select_one('table')
    if table is None:
        raise ParseError('ScholarOne 作者稿件表格未识别。')
    headings = table.select('thead th, thead td')
    if not headings:
        first = table.find('tr')
        candidate = first.find_all(['th', 'td'], recursive=False) if first else []
        labels = {HEADERS.get(_header(h.get_text(' ', strip=True))) for h in candidate}
        if {'manuscript_number', 'title', 'status'} <= labels:
            headings = candidate
    columns = [_header(h.get_text(' ', strip=True)) for h in headings]
    rows = table.select('tr[id^="queue_"]')
    if not rows:
        rows = [r for r in table.select('tbody > tr') if r.find_all('td', recursive=False)]
    journal = page_journal_name(soup, code)
    found = []
    for row in rows:
        cells = row.find_all('td', recursive=False)
        if row in [h.parent for h in headings]:
            continue
        if len(cells) == 1 and EMPTY.search(cells[0].get_text(' ', strip=True)):
            continue
        data = {}
        for position, cell in enumerate(cells):
            label = _header(cell.get('data-title') or cell.get('data-label')
                            or (columns[position] if position < len(columns) else ''))
            field = HEADERS.get(label)
            if field:
                data[field] = _cell_text(cell, field)
        if not all(data.get(field) for field in ('manuscript_number', 'title', 'status')):
            raise ParseError('ScholarOne 稿件行缺少编号、标题或状态，保留旧记录。')
        _status_date(data)
        number = re.sub(r'^(?:ID|Manuscript ID)\s*:\s*', '', data['manuscript_number'], flags=re.I)
        if not number or len(number) > 200:
            raise ParseError('ScholarOne 稿件编号无法识别。')
        found.append(SubmissionStatus(site='scholarone', source=journal or code.upper(),
            manuscript_number=number, title=data['title'], status=data['status'],
            status_date=data.get('status_date') or None,
            submission_date=data.get('submission_date') or None, detail_url=base_url,
            metadata={'journal_name':journal}).to_dict())
    if not found and not EMPTY.search(table.get_text(' ', strip=True)):
        raise ParseError('ScholarOne 未提供明确空列表标记，保留旧记录。')
    return found


def _safe_location(page, code):
    url = urlsplit(page.url)
    if (url.scheme != 'https' or url.hostname != 'mc.manuscriptcentral.com'
            or url.port not in (None, 443) or url.username or url.password
            or url.path.rstrip('/') != '/' + code):
        raise AuthenticationError('ScholarOne 登录地址发生变化，停止提交凭据。')


def _visible(locator):
    for index in range(locator.count()):
        item = locator.nth(index)
        if item.is_visible():
            return item
    return None


def _nav(page, label):
    # Only the named Author/queue navigation is eligible; never manuscript actions.
    pattern = re.compile(r'^[^\w]*(?:\d+\s+)?' + re.escape(label)
                         + r'\s*(?:\(\d+\)|\d+)?[^\w]*$', re.I)
    for role in ('link', 'button', 'tab', 'menuitem'):
        found = _visible(page.get_by_role(role, name=pattern))
        if found is not None:
            return found
    return None


def _settle(page):
    from playwright.sync_api import TimeoutError as BrowserTimeout
    page.wait_for_load_state('domcontentloaded', timeout=30000)
    try:
        page.wait_for_load_state('networkidle', timeout=15000)
    except BrowserTimeout:
        raise requests.Timeout('ScholarOne 稿件页面尚未加载完成。') from None
    check_page(page.content())


def _click_read_nav(page, control):
    # ScholarOne submits a legacy form after a JS click; waiting on an already
    # idle old document can race the new navigation.
    with page.expect_navigation(wait_until='domcontentloaded', timeout=30000):
        control.click()
    _settle(page)


def _read_queue(page, base_url, code):
    rows = []
    seen_pages = set()
    for _ in range(50):
        _safe_location(page, code)
        html = page.content()
        soup = check_page(html)
        queue = soup.select_one('#authorDashboardQueue')
        signature = str(queue)
        if signature in seen_pages:
            raise ParseError('ScholarOne 分页没有更新，保留旧记录。')
        seen_pages.add(signature)
        rows.extend(parse_author_page(html, base_url))
        pager = page.locator('#authorDashboardQueue, .pagination, .pager')
        next_page = _visible(pager.get_by_role('link', name=re.compile(r'^Next(?: page)?\s*(?:[»›>])?$', re.I)))
        if next_page is None:
            next_page = _visible(pager.get_by_role('button', name=re.compile(r'^Next(?: page)?$', re.I)))
        if next_page is None or not next_page.is_enabled() or next_page.evaluate(
                "e=>e.getAttribute('aria-disabled')==='true'||!!e.closest('.disabled')"):
            return rows
        _click_read_nav(page, next_page)
    raise ParseError('ScholarOne 分页超过限制，本轮未保存。')


def fill_login_form(page, account, submit=False):
    """Only fill an identified official form; never interact with a security challenge."""
    _, code = scholarone_url(account['base_url'])
    html = page.content()
    soup = BeautifulSoup(html, 'lxml')
    if _challenge(soup):
        raise ChallengeError('请先在浏览器完成官方安全验证。')
    _safe_location(page, code)
    username = _visible(page.locator('input[name="USERID"]'))
    password = _visible(page.locator('input[name="PASSWORD"][type="password"]'))
    if username is None or password is None:
        raise AuthenticationError('ScholarOne 登录输入框未识别。')
    action = urlsplit(password.evaluate('e=>e.form ? e.form.action : location.href'))
    if (action.scheme != 'https' or action.hostname != 'mc.manuscriptcentral.com'
            or action.port not in (None, 443) or action.username or action.password
            or action.path.rstrip('/') != '/' + code):
        raise AuthenticationError('ScholarOne 登录表单指向外部地址，停止提交凭据。')
    reject = _visible(page.locator('#onetrust-reject-all-handler, #onetrust-close-btn-container button'))
    if reject is not None:
        reject.click()
    username.fill(account['username'])
    password.fill(account['password'])
    if not submit:
        return
    _safe_location(page, code)
    login = _visible(page.locator('#logInButton')) or _nav(page, 'Log In')
    if login is None:
        raise AuthenticationError('ScholarOne 登录按钮未识别。')
    _click_read_nav(page, login)


def authenticated_page(page, account):
    _, code = scholarone_url(account['base_url'])
    _safe_location(page, code)
    check_page(page.content())
    if not (page.locator('#authorDashboardQueue').count() or _nav(page, 'Author')
            or _nav(page, 'Author Center') or _nav(page, 'Submitted Manuscripts')):
        raise AuthenticationError('请登录至官方主页或作者中心后保存会话。')


def read_scholarone(page, account, on_authenticated=None):
    base_url, code = scholarone_url(account['base_url'])
    response = page.goto(base_url, wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(1000)
    html = page.content()
    # check_page rejects a login form by design; validate it via fill_login_form instead.
    if _visible(page.locator('input[name="USERID"]')) is not None:
        fill_login_form(page, account, submit=True)
    else:
        check_page(html)
        if response and response.status >= 400:
            raise requests.RequestException('ScholarOne 登录入口连接失败。')
        authenticated_page(page, account)
    _safe_location(page, code)
    author = _nav(page, 'Author') or _nav(page, 'Author Center')
    if author is not None:
        if on_authenticated is not None:
            on_authenticated()
        _click_read_nav(page, author)
    elif not page.locator('#authorDashboardQueue').count():
        raise AuthenticationError('ScholarOne 登录未确认或需要额外验证。')
    if on_authenticated is not None:
        on_authenticated()
    # Co-authors may have no Submitted Manuscripts link at all. Accept the
    # recognized current queue and visit every other visible read-only queue.
    recognised = bool(page.locator('#authorDashboardQueue').count())
    rows = _read_queue(page, base_url, code) if recognised else []
    for label in QUEUES:
        queue = _nav(page, label)
        if queue is not None:
            recognised = True
            _click_read_nav(page, queue)
            rows.extend(_read_queue(page, base_url, code))
    if not recognised:
        raise ParseError('ScholarOne 作者稿件队列未识别，本轮不保存。')
    result = {}
    for row in rows:
        result.setdefault(row['manuscript_number'], row)
    return list(result.values())


def fetch_scholarone(account, network):
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout, Error as BrowserError
    from .orcid import browser_proxy
    import os
    import tempfile
    from contextlib import nullcontext
    from pathlib import Path
    from .browser_profiles import (profile_path, profile_lock, virtual_display, tools_ready,
                                   secure_profile, save_browser_state, restore_browser_state)
    root = Path(network.get('_runtime_root') or os.environ.get('JOURNALCHECK_RUNTIME', '.runtime')).resolve()
    profile = profile_path(root, account)
    checks = root / 'browser-checks'
    checks.mkdir(mode=0o700, exist_ok=True)
    with profile_lock(profile), tempfile.TemporaryDirectory(prefix='', dir=checks) as directory:
        display = virtual_display(root, directory) if tools_ready(root) else nullcontext(None)
        with display as environment, sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile),
                headless=environment is None, proxy=browser_proxy(network),
                env={**os.environ, **(environment or {})}, viewport={'width':1280, 'height':720},
                args=['--no-sandbox', '--window-size=1280,800'])
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            try:
                restore_browser_state(context, profile, root)
                rows = read_scholarone(page, account,
                    on_authenticated=lambda:save_browser_state(context, profile, root))
                save_browser_state(context, profile, root)
                return rows
            except BrowserTimeout:
                raise requests.Timeout('ScholarOne 登录或列表加载超时。') from None
            except BrowserError:
                raise requests.RequestException('ScholarOne 浏览器连接未完成。') from None
            finally:
                context.close()
                secure_profile(profile)
