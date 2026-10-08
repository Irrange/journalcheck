"""Check rendered geometry with long platform statuses; isolated data, no journal login."""
from pathlib import Path
import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from journalcheck.server.store import Store, DEFAULTS
from playwright.sync_api import sync_playwright

STATUSES = [
    "We've received your submission and are now running technical checks",
    'Your manuscript is being assessed by the editorial team before external peer review can begin',
    '编辑部正在进行稿件技术检查并审核投稿材料，完成后将联系作者或安排后续评审。' * 3,
    'AwaitingEditorialAssessment' * 12,
    'Under Review',
    'Accepted for publication',
]
GEOMETRY = """() => {
  const failures = [];
  for (const card of document.querySelectorAll('.view:not(.hidden) .article-card')) {
    const outer = card.getBoundingClientRect();
    if (outer.left < -1 || outer.right > innerWidth + 1) failures.push('article leaves viewport');
    for (const cell of card.querySelectorAll('.article-metrics > div')) {
      const bounds = cell.getBoundingClientRect();
      for (const pill of cell.querySelectorAll('.status-pill')) {
        const rect = pill.getBoundingClientRect();
        if (rect.left < bounds.left - 1 || rect.right > bounds.right + 1 ||
            rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1 ||
            pill.scrollWidth > pill.clientWidth + 1) {
          failures.push({issue: 'status leaves its metric cell', status: pill.textContent.slice(0, 100),
                         pillWidth: rect.width, cellWidth: bounds.width});
        }
      }
    }
    for (const control of card.querySelectorAll('.article-actions .button')) {
      const rect = control.getBoundingClientRect();
      if (rect.left < outer.left - 1 || rect.right > outer.right + 1)
        failures.push('article action leaves card');
    }
  }
  for (const pill of document.querySelectorAll('.view:not(.hidden) td .status-pill')) {
    const cell = pill.closest('td').getBoundingClientRect(), rect = pill.getBoundingClientRect();
    if (rect.left < cell.left - 1 || rect.right > cell.right + 1 || pill.scrollWidth > pill.clientWidth + 1)
      failures.push('archive status leaves table cell');
  }
  for (const container of document.querySelectorAll('dialog[open],dialog[open] .dialog-card')) {
    if (container.scrollWidth > container.clientWidth + 1) failures.push('dialog has horizontal overflow');
  }
  const close = document.querySelector('dialog[open] .icon-button');
  if (close) {
    const rect = close.getBoundingClientRect();
    if (rect.left < 0 || rect.right > innerWidth) failures.push('dialog close button leaves viewport');
  }
  if (document.documentElement.scrollWidth > innerWidth + 1) failures.push('horizontal page overflow');
  return failures;
}"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reproduce', action='store_true', help='Capture the pre-fix geometry failure.')
    args = parser.parse_args()
    screenshots = REPO / '.runtime/qa/layout'
    screenshots.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='journalcheck-layout-') as directory:
        root = Path(directory)
        store = Store(root)
        store.bootstrap('layout-test@example.test')
        password = (root / 'initial-password.txt').read_text().strip()
        store.set_meta('must_change_password', '0')
        urls = [f'https://submission.springernature.com/submission-details/{i:08d}-1111-1111-1111-111111111111' for i in range(1, 7)]
        account = store.save_account({'name': '布局验证账号', 'platform': 'bmc',
                                     'username': 'layout@example.test', 'password': 'synthetic-only',
                                     'submission_urls': urls})
        rows = [{'site': 'bmc', 'source': 'Synthetic Journal', 'manuscript_number': f'LAYOUT-{i}',
                 'title': 'A long manuscript title describing nutrition and cardiometabolic outcomes across multiple cohorts: ' + 'unbrokenTitleSegment' * 8,
                 'status': status, 'status_date': None, 'reviewer_invited': None,
                 'reviewer_accepted': 3, 'review_reports_received': 2,
                 'detail_url': url, 'metadata': {'tracking_url': url}}
                for i, (status, url) in enumerate(zip(STATUSES, urls))]
        store.apply_rows(account, rows, DEFAULTS, verified=True,
                         target_results=[{'url': url, 'ok': True} for url in urls])
        with store.db() as db:
            db.execute('UPDATE submissions SET archived=1,manual_archived=1 WHERE manuscript_number=?', ('LAYOUT-1',))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        store.set_meta('public_origin', origin)
        process = subprocess.Popen([str(REPO / '.venv/bin/python'), '-m', 'journalcheck.server', 'web',
                                    '--runtime', directory, '--port', str(port)], cwd=REPO,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError('Isolated server did not start')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
                context = browser.new_context(viewport={'width': 1920, 'height': 1080},
                                              extra_http_headers={'Tailscale-User-Login': 'layout-test@example.test'})
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(origin + '/journal/')
                page.locator('#login-form [name=password]').fill(password)
                page.locator('#login-form button').click()
                page.locator('#overview-groups .article-card').first.wait_for()
                widths = [1920] if args.reproduce else [1920, 1440, 1200, 1151, 1150, 1050, 721, 720, 390, 360, 320]
                findings = []
                checks = 0
                for view in ['overview', 'management', 'archive', 'detail']:
                    if view == 'management':
                        page.locator('#overview-groups [data-action=manage-account]').click()
                        page.locator('.management-detail .article-card').first.wait_for()
                    elif view == 'archive':
                        page.locator('#primary-nav a[data-tab=archive]').click()
                        page.locator('#view-archive .text-button').first.wait_for()
                    elif view == 'detail':
                        page.locator('#primary-nav a[data-tab=submissions]').click()
                        page.locator('#overview-groups .article-main [data-action=detail]').first.click()
                        page.locator('#submission-dialog .detail-grid').wait_for()
                    for width in widths:
                        page.set_viewport_size({'width': width, 'height': 1080})
                        # Let the existing view entrance transform finish before measuring.
                        page.wait_for_timeout(250)
                        failures = page.evaluate(GEOMETRY)
                        checks += 1
                        if failures:
                            findings.append({'view': view, 'width': width, 'failures': failures})
                        if width in [1920, 390]:
                            suffix = 'before' if args.reproduce else 'after'
                            page.screenshot(path=str(screenshots / f'{view}-{width}-{suffix}.png'), full_page=True)
                            if view == 'overview':
                                page.locator('#overview-groups .article-card').first.screenshot(path=str(screenshots / f'article-{width}-{suffix}.png'))
                browser.close()
                assert not errors, errors
                if args.reproduce:
                    assert findings, 'Expected the reported overflow to reproduce'
                    print('Reproduced long status overflow:', json.dumps(findings[0], ensure_ascii=False))
                else:
                    assert not findings, json.dumps(findings, ensure_ascii=False)
                    print(f'Layout passed: {checks} view/viewport combinations; long English, Chinese, unbroken text, titles and action controls stay contained.')
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
