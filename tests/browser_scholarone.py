"""Focused ScholarOne UI check using an isolated store and synthetic data only."""
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from journalcheck.server.store import DEFAULTS, Store
from playwright.sync_api import sync_playwright


JOURNAL_URL = 'https://mc.manuscriptcentral.com/ageing'
SYNTHETIC_PASSWORDS = ('synthetic-scholarone-one', 'synthetic-scholarone-two')
ARTICLE_TITLE = 'Synthetic ScholarOne manuscript for browser check'


def assert_no_horizontal_overflow(page, width):
    dimensions = page.evaluate('''() => ({
      viewport: innerWidth,
      document: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      openDialogs: [...document.querySelectorAll('dialog[open]')].map(dialog => ({
        width: dialog.clientWidth,
        scrollWidth: dialog.scrollWidth,
        cardWidth: dialog.querySelector('.dialog-card')?.clientWidth,
        cardScrollWidth: dialog.querySelector('.dialog-card')?.scrollWidth,
      })),
    })''')
    assert dimensions['viewport'] == width, dimensions
    assert dimensions['document'] <= width and dimensions['body'] <= width, dimensions
    for dialog in dimensions['openDialogs']:
        assert dialog['scrollWidth'] <= dialog['width'] + 1, dimensions
        assert dialog['cardScrollWidth'] <= dialog['cardWidth'] + 1, dimensions


def main():
    with tempfile.TemporaryDirectory(prefix='journalcheck-scholarone-') as directory:
        root = Path(directory)
        store = Store(root)
        store.bootstrap('scholarone-browser@example.test')
        password = (root / 'initial-password.txt').read_text(encoding='utf-8').strip()
        store.set_meta('must_change_password', '0')

        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        store.set_meta('public_origin', origin)
        process = subprocess.Popen(
            [str(REPO / '.venv/bin/python'), '-m', 'journalcheck.server', 'web',
             '--runtime', directory, '--port', str(port)],
            cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError('Isolated ScholarOne test server failed to start')

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
                context = browser.new_context(
                    viewport={'width': 1440, 'height': 1000},
                    extra_http_headers={'Tailscale-User-Login': 'scholarone-browser@example.test'},
                )
                page = context.new_page()
                errors = []
                external_requests = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: external_requests.append(request.url)
                        if not request.url.startswith(origin + '/') else None)
                page.goto(origin + '/journal/')
                page.locator('#login-form [name=password]').fill(password)
                page.locator('#login-form button').click()
                page.locator('#workspace').wait_for(state='visible')
                page.locator('#primary-nav a[data-tab=accounts]').click()
                page.locator('#view-accounts [data-action=new-account]').click()

                form = page.locator('#account-form')
                form.locator('[name=platform]').select_option('scholarone')
                assert form.locator('.field-username').is_visible()
                assert form.locator('.field-password').is_visible()
                assert form.locator('.field-base-url').is_visible()
                assert form.locator('.field-journal-name').is_visible()
                assert form.locator('.field-login-method').is_hidden()
                assert form.locator('[name=journal_code]').count() == 0
                assert form.locator('[name=username]').evaluate('(el) => el.required')
                assert form.locator('[name=password]').evaluate('(el) => el.required')
                assert form.locator('[name=base_url]').evaluate('(el) => el.required')
                assert form.locator('[name=journal_name]').evaluate('(el) => !el.required')
                assert form.locator('[name=base_url]').get_attribute('placeholder') == JOURNAL_URL
                assert '自动识别' in form.locator('#base-url-help').inner_text()
                assert_no_horizontal_overflow(page, 1440)

                form.locator('[name=name]').fill('ScholarOne login one')
                form.locator('[name=username]').fill('scholarone-one@example.test')
                form.locator('[name=password]').fill(SYNTHETIC_PASSWORDS[0])
                form.locator('[name=base_url]').fill(JOURNAL_URL)
                form.get_by_role('button', name='保存账号', exact=True).click()
                page.locator('.management-detail').wait_for()
                first = next(item for item in store.accounts(True)
                             if item['username'] == 'scholarone-one@example.test')
                assert first['journal_code'] == 'ageing', first
                assert first['base_url'] == JOURNAL_URL, first

                # Seed a manuscript directly; no checker or journal request is started.
                store.apply_rows(first, [{
                    'site': 'scholarone', 'source': 'ageing', 'manuscript_number': 'SYNTH-S1-001',
                    'title': ARTICLE_TITLE, 'status': 'Under Review', 'status_date': None,
                    'reviewer_invited': None, 'reviewer_accepted': None,
                    'review_reports_received': None, 'review_comments': [],
                    'detail_url': JOURNAL_URL, 'metadata': {},
                }], DEFAULTS, verified=True)

                page.locator(f'[data-action=edit-account][data-id="{first["id"]}"]').click()
                form = page.locator('#account-form')
                assert form.locator('[name=password]').evaluate('(el) => el.required') is False
                form.locator('[name=password]').fill('')
                form.get_by_role('button', name='保存账号', exact=True).click()
                page.locator('.management-detail').wait_for()
                assert store.account(first['id'], True)['password'] == SYNTHETIC_PASSWORDS[0]
                assert store.account(first['id'], True)['journal_code'] == 'ageing'

                # Add a second synthetic login through the same form for the same journal.
                page.locator('[data-action=back-management]').click()
                page.locator('#view-accounts [data-action=new-account]').click()
                form = page.locator('#account-form')
                form.locator('[name=platform]').select_option('scholarone')
                form.locator('[name=name]').fill('ScholarOne login two')
                form.locator('[name=username]').fill('scholarone-two@example.test')
                form.locator('[name=password]').fill(SYNTHETIC_PASSWORDS[1])
                form.locator('[name=base_url]').fill(JOURNAL_URL)
                form.get_by_role('button', name='保存账号', exact=True).click()
                page.locator('.management-detail').wait_for()
                second = next(item for item in store.accounts(True)
                              if item['username'] == 'scholarone-two@example.test')
                assert second['journal_code'] == 'ageing', second

                page.locator('[data-action=back-management]').click()
                group = page.locator('#view-accounts .source-group').filter(has_text='Age and Ageing')
                group.wait_for(state='visible')
                assert group.count() == 1
                assert group.locator('.management-card').count() == 2
                page.locator(f'#view-accounts [data-action=manage-account][data-id="{first["id"]}"]').click()
                switcher = page.locator('.account-switcher')
                switcher.wait_for(state='visible')
                switcher.locator(f'[data-action=manage-account][data-id="{second["id"]}"]').click()
                assert second['id'] in page.locator('.management-detail').get_attribute('data-account-id')
                assert 'scholarone-two@example.test' in page.locator('.management-detail').inner_text()

                page.locator('[data-action=back-management]').click()
                page.locator('#primary-nav a[data-tab=submissions]').click()
                page.get_by_role('button', name=ARTICLE_TITLE, exact=True).wait_for()
                page.get_by_label('按平台筛选').select_option('scholarone')
                assert page.locator('#overview-groups .source-group').count() == 1
                assert page.get_by_role('button', name=ARTICLE_TITLE, exact=True).is_visible()
                page.set_viewport_size({'width': 390, 'height': 844})
                assert_no_horizontal_overflow(page, 390)

                page.locator('#primary-nav a[data-tab=accounts]').click()
                page.locator('#view-accounts [data-action=new-account]').click()
                form = page.locator('#account-form')
                form.locator('[name=platform]').select_option('scholarone')
                assert_no_horizontal_overflow(page, 390)
                assert not errors, errors
                assert not external_requests, external_requests
                browser.close()

        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    print('ScholarOne browser check passed: form, derived code, password preservation, journal grouping, article filter, desktop/390px layout; no external requests.')


if __name__ == '__main__':
    main()
