"""Real portal + Caddy + journal UI with synthetic credentials, never production."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

REPO=Path(__file__).resolve().parents[1]
PORTAL=REPO.parent/'server-portal'
sys.path[:0]=[str(REPO),str(PORTAL)]
from auth.store import Store as PortalStore
from journalcheck.server.store import Store
from playwright.sync_api import sync_playwright


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix='jc-vault-') as tmp:
        root=Path(tmp);pr=root/'portal';jr=root/'journal';ps=PortalStore(pr)
        password=ps.initialize('vault@example.test');js=Store(jr);js.bootstrap('vault@example.test')
        secret='synthetic-vault-secret!';a=js.save_account({'name':'Synthetic BMC account','platform':'bmc','username':'vault@example.test','password':secret})
        b=js.save_account({'name':'Archived account','platform':'scholarone','base_url':'https://mc.manuscriptcentral.com/ageing','username':'archived@example.test','password':'archived-synthetic-secret'})
        with js.db() as db:db.execute('UPDATE accounts SET archived=1 WHERE id=?',(b['id'],))
        gp,ap,jp=port(),port(),port();origin=f'http://127.0.0.1:{gp}';js.set_meta('public_origin',origin)
        from auth.security import load_or_create_key
        key=pr/'journal-gateway.key';load_or_create_key(key)
        config=(PORTAL/'Caddyfile').read_text().replace(':18200',f':{gp}').replace(':18201',f':{ap}').replace(':18181',f':{jp}')
        (root/'Caddyfile').write_text(config)
        commands=[([sys.executable,'-m','auth','--host','127.0.0.1','--port',str(ap)],PORTAL,{'PORTAL_AUTH_RUNTIME':str(pr),'PORTAL_AUTH_ORIGIN':origin}),
                  ([sys.executable,'-m','journalcheck.server','web','--runtime',str(jr),'--port',str(jp)],REPO,{'JOURNALCHECK_GATEWAY_MODE':'1','JOURNALCHECK_GATEWAY_KEY':str(key)}),
                  ([str(PORTAL/'.runtime/bin/caddy'),'run','--config',str(root/'Caddyfile'),'--adapter','caddyfile'],PORTAL,{})]
        processes=[]
        try:
            for command,cwd,env in commands:processes.append(subprocess.Popen(command,cwd=cwd,env={**os.environ,**env},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
            for p in (gp,ap,jp):
                for _ in range(100):
                    try:
                        with socket.create_connection(('127.0.0.1',p),.1):break
                    except OSError:time.sleep(.1)
                else:raise AssertionError('Isolated server not ready')
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,args=['--no-sandbox']);context=browser.new_context(permissions=['clipboard-read','clipboard-write']);page=context.new_page()
                errors=[];reveal_calls=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda r:reveal_calls.append(r.url) if r.url.endswith('/reveal') else None)
                page.goto(origin+'/login')
                # Use the real auth endpoint while keeping the generated password out of argv/logs.
                result=page.request.post(origin+'/portal-auth/login',headers={'Origin':origin},data={'password':password});assert result.status==200
                page.goto(origin+'/journal/#vault')
                card=page.locator(f'.vault-card[data-account-id="{a["id"]}"]');card.wait_for()
                assert not reveal_calls and secret not in page.content()
                for width in (1440,390):
                    page.set_viewport_size({'width':width,'height':900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    if width==390:
                        assert page.evaluate('''() => [...document.querySelectorAll('#primary-nav a')].every(e=>{
                            const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;
                        })''')
                    card.get_by_role('button',name='查看密码',exact=True).click()
                    form=page.locator('#vault-unlock-form');form.locator('[name=password]').fill('wrong-password');form.get_by_role('button',name='解锁',exact=True).click()
                    page.get_by_text('门户密码不正确。',exact=True).wait_for()
                    assert secret not in page.content()
                    form.locator('[name=password]').fill(password);form.get_by_role('button',name='解锁',exact=True).click()
                    page.locator('#vault-secret-dialog[open]').wait_for();assert page.locator('#vault-secret-value').input_value()==secret
                    assert form.locator('[name=password]').input_value()==''
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.get_by_role('button',name='隐藏并关闭',exact=True).click()
                    assert page.locator('#vault-secret-value').input_value()==''
                    card.get_by_role('button',name='复制密码',exact=True).click();page.get_by_text('密码已复制到剪贴板。',exact=True).wait_for()
                    assert page.evaluate('navigator.clipboard.readText()')==secret
                    assert page.locator('#vault-secret-value').input_value()==''
                    page.get_by_role('button',name='立即锁定',exact=True).click();page.get_by_text('密码库已锁定。',exact=True).wait_for()
                # Search and archived entries remain available without exposing passwords.
                page.get_by_label('搜索密码库').fill('Archived');assert page.locator('.vault-card').count()==1
                page.get_by_label('搜索密码库').fill('');card.get_by_role('button',name='编辑',exact=True).click()
                form.locator('[name=password]').fill(password);form.get_by_role('button',name='解锁',exact=True).click()
                account=page.locator('#account-form');account.wait_for(state='visible');account.locator('[name=password]').fill('updated-synthetic-vault-secret');account.get_by_role('button',name='保存账号').click()
                page.locator('#account-dialog').wait_for(state='hidden')
                card=page.locator(f'.vault-card[data-account-id="{a["id"]}"]');card.wait_for();card.get_by_role('button',name='查看密码',exact=True).click()
                page.locator('#vault-secret-dialog[open]').wait_for();assert page.locator('#vault-secret-value').input_value()=='updated-synthetic-vault-secret'
                # A real 30-second timeout clears both the dialog and the value.
                page.locator('#vault-secret-dialog').wait_for(state='hidden',timeout=35000)
                assert page.locator('#vault-secret-value').input_value()==''
                card.get_by_role('button',name='查看密码',exact=True).click();page.locator('#vault-secret-dialog[open]').wait_for()
                page.keyboard.press('Escape');page.locator('#primary-nav a[data-tab=submissions]').click()
                assert page.locator('#vault-secret-value').input_value()==''
                # Expire the real grant server-side and ensure it cannot reveal again.
                page.locator('#primary-nav a[data-tab=vault]').click();card.wait_for()
                with ps.db() as db:db.execute('UPDATE vault_unlocks SET expires_at=0')
                card.get_by_role('button',name='查看密码',exact=True).click();page.locator('#vault-unlock-dialog[open]').wait_for()
                assert page.locator('#vault-secret-value').input_value()==''
                assert not errors,errors
                assert page.evaluate('Object.values(localStorage).join(" ")+Object.values(sessionStorage).join(" ")').find(secret)==-1
                page.get_by_role('button',name='取消',exact=True).click()
                qa=REPO/'.runtime/qa/vault';qa.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(qa/'vault-mobile.png'),full_page=True)
                browser.close()
        finally:
            for process in reversed(processes):
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
    print('Vault browser integration passed: real portal reauth, gateway routing, desktop/390px, copy, edit, 30s hide, expiry and plaintext cleanup; synthetic accounts only.')


if __name__=='__main__':main()
