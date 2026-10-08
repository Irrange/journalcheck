"""Isolated ScholarOne browser-session UI check; never contacts the journal."""
from pathlib import Path
import json
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


def assert_no_horizontal_overflow(page, width):
    dimensions = page.evaluate('''() => ({viewport: innerWidth,
      document: document.documentElement.scrollWidth, body: document.body.scrollWidth,
      dialog: [...document.querySelectorAll('dialog[open]')].map(d => [d.clientWidth, d.scrollWidth])})''')
    assert dimensions['viewport'] == width, dimensions
    assert dimensions['document'] <= width and dimensions['body'] <= width, dimensions
    assert all(scroll <= client + 1 for client, scroll in dimensions['dialog']), dimensions


def main():
    with tempfile.TemporaryDirectory(prefix='journalcheck-scholarone-session-') as directory:
        root = Path(directory)
        store = Store(root)
        store.bootstrap('session-ui@example.test')
        password = (root / 'initial-password.txt').read_text(encoding='utf-8').strip()
        store.set_meta('must_change_password', '0')
        account = store.save_account({
            'name': 'Synthetic ageing account', 'platform': 'scholarone',
            'username': 'session-ui@example.test', 'password': 'synthetic-only-password',
            'base_url': JOURNAL_URL,
        })
        store.apply_rows(account, [{
            'site': 'scholarone', 'source': 'AGEING', 'manuscript_number': 'SYNTH-S1-SESSION',
            'title': 'Synthetic ScholarOne browser session article', 'status': 'Under Review',
            'status_date': None, 'reviewer_invited': None, 'reviewer_accepted': None,
            'review_reports_received': None, 'review_comments': [], 'detail_url': JOURNAL_URL,
            'metadata': {'journal_name': 'Author Center'},
        }], DEFAULTS, verified=True)

        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        store.set_meta('public_origin', origin)
        process = subprocess.Popen(
            [str(REPO / '.venv/bin/python'), '-m', 'journalcheck.server', 'web',
             '--runtime', directory, '--port', str(port)], cwd=REPO,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError('Isolated test server failed to start')

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
                context = browser.new_context(
                    viewport={'width': 1440, 'height': 1000},
                    extra_http_headers={'Tailscale-User-Login': 'session-ui@example.test'},
                )
                api_calls = []
                external_requests = []
                context.route('**/api/accounts/*/browser-session', lambda route: (
                    api_calls.append(route.request.method),
                    route.fulfill(status=200, content_type='application/json', body=json.dumps(
                        {'status': 'idle', 'message': '尚未启动浏览器登录。'} if route.request.method == 'GET'
                        else {'id': 'synthetic-session', 'account_id': account['id'], 'status': 'pending',
                              'message': '请在新标签页完成登录。',
                              'url': '/journal/browser-login/synthetic-session',
                              'expires_at': '2099-01-01T00:00:00Z'}, ensure_ascii=False,
                    )),
                ))
                page = context.new_page()
                page.on('request', lambda request: external_requests.append(request.url)
                        if not request.url.startswith(origin + '/') else None)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(origin + '/journal/')
                page.locator('#login-form [name=password]').fill(password)
                page.locator('#login-form button').click()
                page.locator('#workspace').wait_for(state='visible')

                group = page.locator('#overview-groups .source-group')
                group.get_by_role('heading', name='Age and Ageing').wait_for()
                group.get_by_role('button', name='登录／更新会话').wait_for()
                page.locator('#primary-nav a[data-tab=accounts]').click()
                page.locator('#view-accounts .management-card').get_by_role(
                    'button', name='登录／更新会话').wait_for()
                page.locator('#view-accounts [data-action=manage-account]').click()
                detail = page.locator('.management-detail')
                detail.get_by_role('heading', name='Age and Ageing').wait_for()
                detail.get_by_role('button', name='登录／更新会话').wait_for()
                assert '即可自动登录并获取稿件' in detail.inner_text()
                assert '仅当平台要求额外验证时' in detail.inner_text()
                assert '验证并启用' in detail.inner_text()
                assert '检查失败会保留已有稿件' in detail.inner_text()
                assert '尚未启动浏览器登录' in detail.inner_text()

                page.set_viewport_size({'width': 390, 'height': 844})
                assert_no_horizontal_overflow(page, 390)
                with page.expect_popup() as popup_info:
                    detail.get_by_role('button', name='登录／更新会话').click()
                popup = popup_info.value
                popup.wait_for_url('**/journal/browser-login/synthetic-session')
                assert popup.url.startswith(origin + '/journal/browser-login/')
                assert api_calls == ['GET', 'POST'], api_calls
                assert_no_horizontal_overflow(page, 390)
                assert not external_requests, external_requests
                assert not errors, errors
                browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    print('ScholarOne browser-session UI passed at desktop and 390px using a temporary store and synthetic API responses; no journal requests were sent.')


if __name__ == '__main__':
    main()
