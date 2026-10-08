"""Browser regressions against an isolated runtime with synthetic sample data."""
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from journalcheck.server.store import Store, DEFAULTS
from journalcheck.server.worker import Worker
from playwright.sync_api import sync_playwright


def bmc_url(number):
    return f'https://submission.springernature.com/submission-details/{number:08d}-1111-1111-1111-111111111111'


def seed_row(number, title, url):
    return {
        'site': 'bmc', 'source': 'BMC Public Health', 'manuscript_number': number,
        'title': title, 'status': 'Under Review', 'status_date': '2026-09-28',
        'reviewer_invited': 3, 'reviewer_accepted': 2, 'review_reports_received': 1,
        'review_comments': ['Synthetic review note for browser checks.'],
        'detail_url': url, 'metadata': {'tracking_url': url},
    }


def main():
    screenshots = REPO / '.runtime/qa'
    screenshots.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='journalcheck-browser-') as directory:
        root = Path(directory)
        store = Store(root)
        store.bootstrap('browser-test@example.test')
        password = (root / 'initial-password.txt').read_text(encoding='utf-8').strip()
        store.set_meta('must_change_password', '0')

        email_urls = [bmc_url(11111111), bmc_url(22222222)]
        email_account = store.save_account({
            'name': 'BMC 邮箱示例', 'platform': 'bmc', 'login_method': 'password',
            'username': 'email-demo@example.test', 'password': 'synthetic-email-password',
            'submission_urls': email_urls,
        })
        store.apply_rows(email_account, [
            seed_row('DEMO-EMAIL-1', 'Synthetic manuscript for management checks', email_urls[0]),
            seed_row('DEMO-EMAIL-2', 'Second synthetic manuscript', email_urls[1]),
        ], DEFAULTS, verified=True,
            target_results=[{'url': url, 'ok': True} for url in email_urls])
        store.apply_rows(email_account, [
            seed_row('DEMO-EMAIL-1', 'Synthetic manuscript for management checks', email_urls[0]) | {'review_reports_received': 2},
        ], DEFAULTS)

        orcid_urls = [bmc_url(33333333), bmc_url(44444444)]
        orcid_account = store.save_account({
            'name': 'BMC ORCID 示例', 'platform': 'bmc', 'login_method': 'orcid',
            'username': '0000-0000-0000-0000', 'password': 'synthetic-orcid-password',
            'submission_urls': orcid_urls,
        })

        em_url = 'https://www.editorialmanager.com/ghrpj/default2.aspx'
        em_accounts = [
            store.save_account({'name': 'EM 登录一', 'journal_name': 'Synthetic Journal Display',
                                'platform': 'em', 'username': 'em-one@example.test', 'password': 'synthetic-em-one',
                                'base_url': em_url}),
            store.save_account({'name': 'EM 登录二', 'journal_name': 'Synthetic Journal Display',
                                'platform': 'em', 'username': 'em-two@example.test', 'password': 'synthetic-em-two',
                                'base_url': em_url}),
        ]

        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        store.set_meta('public_origin', origin)
        process = subprocess.Popen(
            [str(REPO / '.venv/bin/python'), '-m', 'journalcheck.server', 'web', '--runtime', directory, '--port', str(port)],
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
                raise AssertionError('Smoke web server failed to start')

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
                context = browser.new_context(
                    viewport={'width': 1440, 'height': 1000},
                    extra_http_headers={'Tailscale-User-Login': 'browser-test@example.test'},
                )
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(origin + '/journal/')
                assert page.locator('link[rel=icon]').get_attribute('href') == '/journal/static/journalcheck.svg'
                assert page.locator('#auth-view img.brand-mark').evaluate('(el)=>el.complete && el.naturalWidth>0')
                page.locator('#login-form [name=password]').fill('wrong-synthetic-password')
                page.locator('#login-form button').click()
                page.get_by_text('管理密码不正确。', exact=True).wait_for()
                page.locator('#login-form [name=password]').fill(password)
                page.locator('#login-form button').click()
                page.locator('#workspace').wait_for(state='visible')
                page.get_by_role('button', name='Synthetic manuscript for management checks', exact=True).wait_for()

                # Search, platform and status survive opening management from a manuscript and returning.
                page.get_by_role('searchbox', name='搜索稿件').fill('Synthetic manuscript for management checks')
                page.get_by_label('按平台筛选').select_option('bmc')
                page.get_by_label('按状态筛选').select_option('Under Review')
                page.get_by_role('button', name='Synthetic manuscript for management checks', exact=True).click()
                page.get_by_role('button', name='管理所属账号', exact=True).click()
                page.get_by_role('button', name='返回投稿总览', exact=True).click()
                assert page.get_by_role('searchbox', name='搜索稿件').input_value() == 'Synthetic manuscript for management checks'
                assert page.get_by_label('按平台筛选').input_value() == 'bmc'
                assert page.get_by_label('按状态筛选').input_value() == 'Under Review'

                # The new overview uses article cards; its detail dialog and histories remain usable.
                page.get_by_role('button', name='Synthetic manuscript for management checks', exact=True).click()
                page.get_by_text('Synthetic review note for browser checks.', exact=True).first.wait_for()
                page.get_by_text('状态时间线', exact=True).wait_for()
                page.locator('[data-close-dialog=submission-dialog]').click()

                # Preserve login, CSRF-protected settings, and PushPlus behavior from the existing smoke.
                for tab in ('accounts', 'jobs', 'notifications', 'archive', 'settings'):
                    page.locator(f'#primary-nav a[data-tab={tab}]').click()
                    page.wait_for_timeout(100)
                    assert page.locator(f'#view-{tab}').is_visible()
                    assert '请求失败' not in page.locator(f'#view-{tab}').inner_text()
                page.locator('#primary-nav a[data-tab=notifications]').click()
                page.locator('[name=pushplus_token]').fill('synthetic-pushplus-token')
                page.get_by_role('button', name='保存通知设置', exact=True).click()
                page.get_by_text('已配置；留空保留原 Token。', exact=True).wait_for()
                assert store.settings()['pushplus_token'] == 'synthetic-pushplus-token'
                assert page.locator('[name=pushplus_token]').input_value() == ''
                assert page.locator('[name=smtp_host]').count() == 0
                assert page.locator('[name=wecom_webhook]').count() == 0
                page.get_by_role('button', name='PushPlus 邮件', exact=True).click()
                page.get_by_text('测试通知已加入发送队列。', exact=True).wait_for()
                worker = Worker(store, sender=lambda *args: {'status': 'accepted', 'receipt_id': 'synthetic-receipt-123'})
                assert worker.deliver_one()
                page.locator('#primary-nav a[data-tab=settings]').click()
                page.locator('#primary-nav a[data-tab=notifications]').click()
                page.get_by_text('PushPlus 已受理', exact=True).wait_for()
                page.get_by_text('流水号：synthetic-receipt-123', exact=True).wait_for()

                # Existing email and ORCID accounts remain independently editable; blank password retains links and secret.
                page.locator('#primary-nav a[data-tab=accounts]').click()
                page.locator(f'#view-accounts [data-action=manage-account][data-id="{email_account["id"]}"]').click()
                detail = page.locator('.management-detail')
                detail.wait_for(state='visible')
                page.locator(f'[data-action=edit-account][data-id="{email_account["id"]}"]').click()
                form = page.locator('#account-form')
                assert form.locator('[name=password]').evaluate('(el)=>el.required') is False
                form.locator('[name=name]').fill('BMC 邮箱已编辑')
                form.locator('[name=password]').fill('')
                form.get_by_role('button', name='保存账号', exact=True).click()
                detail.wait_for()
                saved_email = store.account(email_account['id'], True)
                assert saved_email['password'] == 'synthetic-email-password'
                assert saved_email['submission_urls'] == email_urls
                assert len(saved_email['targets']) == 2
                page.screenshot(path=str(screenshots / 'desktop.png'), full_page=True)

                page.locator(f'[data-action=back-management]').click()
                page.locator(f'#view-accounts [data-action=manage-account][data-id="{orcid_account["id"]}"]').click()
                page.locator(f'[data-action=edit-account][data-id="{orcid_account["id"]}"]').click()
                form = page.locator('#account-form')
                assert form.locator('[name=login_method]').input_value() == 'orcid'
                form.locator('[name=password]').fill('')
                form.get_by_role('button', name='保存账号', exact=True).click()
                store_orcid = store.account(orcid_account['id'], True)
                assert store_orcid['password'] == 'synthetic-orcid-password'
                assert store_orcid['login_method'] == 'orcid'
                assert store_orcid['submission_urls'] == orcid_urls

                # A saved BMC account can add a batch later; duplicate URL rejection leaves it empty.
                page.locator(f'[data-action=back-management]').click()
                page.locator('[data-action=new-account]').click()
                form = page.locator('#account-form')
                form.locator('[name=name]').fill('BMC 延后添加文章')
                form.locator('[name=platform]').select_option('bmc')
                form.locator('[name=login_method]').select_option('password')
                form.locator('[name=username]').fill('later-demo@example.test')
                form.locator('[name=password]').fill('synthetic-later-password')
                assert form.locator('[name=submission_urls]').count() == 0
                form.get_by_role('button', name='保存账号', exact=True).click()
                page.locator('#account-dialog').wait_for(state='hidden')
                new_account = next(item for item in store.accounts(True) if item['name'] == 'BMC 延后添加文章')
                assert not new_account['submission_urls'] and not new_account['targets']
                page.get_by_role('button', name='添加文章', exact=True).click()
                target_form = page.locator('#target-form')
                added_urls = [bmc_url(55555555), bmc_url(66666666)]
                target_form.locator('[name=urls]').fill('\n'.join([added_urls[0], added_urls[0]]))
                target_form.get_by_role('button', name='添加文章', exact=True).click()
                page.locator('#target-form-error').wait_for(state='visible')
                assert store.account(new_account['id'])['targets'] == []
                target_form.locator('[name=urls]').fill('\n'.join(added_urls))
                target_form.get_by_role('button', name='添加文章', exact=True).click()
                page.locator('#target-dialog').wait_for(state='hidden')
                assert {item['url'] for item in store.account(new_account['id'])['targets']} == set(added_urls)

                # Replacing a single link archives its existing article; stopping the replacement preserves its history.
                page.locator(f'[data-action=back-management]').click()
                page.locator(f'#view-accounts [data-action=manage-account][data-id="{email_account["id"]}"]').click()
                replacement_url = bmc_url(77777777)
                target_card = page.locator(f'.article-card[data-target-id="{store.account(email_account["id"])["targets"][0]["id"]}"]')
                old_submission_id = store.submissions()[0]['id']
                target_card.get_by_role('button', name='修改链接', exact=True).click()
                target_form = page.locator('#target-form')
                target_form.locator('[name=urls]').fill(replacement_url)
                target_form.get_by_role('button', name='保存链接', exact=True).click()
                page.locator('#target-dialog').wait_for(state='hidden')
                assert [item['url'] for item in store.account(email_account['id'])['targets'] if item['url'] == replacement_url]
                assert store.submission(old_submission_id)['submission']['archived'] == 1
                replacement_account = store.account(email_account['id'], True)
                store.apply_rows(replacement_account, [
                    seed_row('DEMO-REPLACEMENT', 'Synthetic replacement article', replacement_url),
                ], DEFAULTS, verified=True, target_results=[{'url': target['url'], 'ok': True} for target in replacement_account['targets']])
                page.locator('[data-action=management-section][data-section=login]').click()
                page.locator('[data-action=management-section][data-section=articles]').click()
                replacement_card = page.locator(f'.article-card[data-target-id="{next(t["id"] for t in store.account(email_account["id"])["targets"] if t["url"] == replacement_url)}"]')
                confirmation = []
                page.once('dialog', lambda dialog: (confirmation.append(dialog.message), dialog.accept()))
                replacement_card.get_by_role('button', name='停止追踪', exact=True).click()
                assert confirmation and '稿件和历史会保留在归档中' in confirmation[0]
                page.get_by_text('已停止追踪，稿件历史保留。', exact=True).wait_for()
                assert store.account(email_account['id'])['targets']
                assert store.submissions(True)
                stopped = next(row for row in store.submissions(True) if row['manuscript_number'] == 'DEMO-REPLACEMENT')
                assert stopped['archived'] == 1
                page.locator('.account-history summary').click()
                history_text = page.locator('.account-history').inner_text()
                assert 'Synthetic replacement article' in history_text

                # EM account grouping uses journal display name and still switches between separate credentials.
                page.locator('[data-action=back-management]').click()
                em_group = page.locator('.source-group').filter(has_text='Synthetic Journal Display')
                em_group.wait_for(state='visible')
                assert em_group.count() == 1
                assert em_group.locator('.management-card').count() == 2
                assert {item['journal_code'] for item in store.accounts(True) if item['platform'] == 'em'} == {'ghrpj'}
                page.locator(f'#view-accounts [data-action=manage-account][data-id="{em_accounts[0]["id"]}"]').click()
                switcher = page.locator('.account-switcher')
                switcher.wait_for(state='visible')
                switcher.locator(f'[data-action=manage-account][data-id="{em_accounts[1]["id"]}"]').click()
                page.locator('[data-action=management-section][data-section=login]').click()
                assert 'em-two@example.test' in page.locator('.management-detail').inner_text()
                assert store.account(em_accounts[0]['id'], True)['password'] == 'synthetic-em-one'
                assert store.account(em_accounts[1]['id'], True)['password'] == 'synthetic-em-two'

                # Archived BMC account keeps profile editing available and stays archived.
                page.locator(f'[data-action=back-management]').click()
                page.locator(f'#view-accounts [data-action=manage-account][data-id="{orcid_account["id"]}"]').click()
                page.locator('.more-actions summary').click()
                page.locator(f'[data-action=archive-account][data-id="{orcid_account["id"]}"]').click()
                page.get_by_text('账号已归档，可以编辑登录资料；恢复后才能添加文章和启用检查。', exact=True).wait_for()
                assert store.account(orcid_account['id'])['archived'] == 1
                page.locator(f'[data-action=edit-account][data-id="{orcid_account["id"]}"]').click()
                page.locator('#account-form [name=name]').fill('已归档 ORCID 账号')
                page.locator('#account-form [name=password]').fill('')
                page.get_by_role('button', name='保存账号', exact=True).click()
                page.locator('#account-dialog').wait_for(state='hidden')
                assert store.account(orcid_account['id'], True)['archived'] == 1
                assert store.account(orcid_account['id'], True)['password'] == 'synthetic-orcid-password'

                # One journal group can contain two accounts, and their management controls stay usable at 390px.
                page.set_viewport_size({'width': 390, 'height': 1050})
                page.locator(f'[data-action=back-management]').click()
                page.locator(f'#view-accounts [data-action=manage-account][data-id="{email_account["id"]}"]').click()
                page.locator('.management-detail').wait_for()
                assert page.locator('[data-action=edit-account]').is_visible()
                assert page.locator('[data-action=add-target]').is_visible()
                article_actions = page.locator('.article-card [data-action=edit-target], .article-card [data-action=remove-target]')
                assert article_actions.count() >= 2
                for index in range(article_actions.count()):
                    assert article_actions.nth(index).is_visible()
                first_article_action = article_actions.first
                first_article_action.scroll_into_view_if_needed()
                action_box = first_article_action.bounding_box()
                assert action_box and action_box['x'] >= 0 and action_box['x'] + action_box['width'] <= 390
                assert action_box['y'] >= 0 and action_box['y'] + action_box['height'] < 1050 - 64
                dimensions = page.evaluate('({width: innerWidth, doc: document.documentElement.scrollWidth, body: document.body.scrollWidth})')
                assert dimensions['doc'] <= dimensions['width'] and dimensions['body'] <= dimensions['width'], dimensions
                controls = page.locator('.management-controls .button')
                for index in range(controls.count()):
                    box = controls.nth(index).bounding_box()
                    assert box and box['x'] >= 0 and box['x'] + box['width'] <= 390
                page.screenshot(path=str(screenshots / 'mobile.png'))

                page.locator('#primary-nav a[data-tab=settings]').click()
                page.locator('[name=interval_minutes]').select_option('120')
                page.locator('[data-form=settings] button[type=submit]').click()
                page.get_by_text('设置已保存。', exact=True).wait_for()
                assert store.settings()['interval_minutes'] == 120
                page.locator('#logout-button').click()
                page.locator('#login-form').wait_for(state='visible')
                assert not errors, errors
                browser.close()
            print('Browser smoke passed: account management, article tracking, EM grouping, filters, auth, PushPlus, desktop/mobile; no real journal logins.')
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
