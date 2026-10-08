"""Official Springer -> ORCID browser OAuth, isolated to one account check.

No persistent login profile or cookie export is saved; browser contexts are disposed.
Passwords, screenshots and response bodies are never logged or exported.
Challenges and account-linking forms require the user; they are never bypassed.
"""
from __future__ import annotations

import re
import time
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs

from .adapters import AuthenticationError,ParseError


def browser_proxy(network):
    raw=network.get('https_proxy') or network.get('http_proxy')
    if not raw:
        return None
    u=urlsplit(raw)
    result={'server':f'{u.scheme}://{u.hostname}'+(f':{u.port}' if u.port else '')}
    if u.username:
        from urllib.parse import unquote
        result.update(username=unquote(u.username),password=unquote(u.password or ''))
    if network.get('no_proxy'):
        result['bypass']=network['no_proxy']
    return result


def login_orcid(page,username,password):
    link=page.locator('#login-via-orcid-url')
    if not link.count():
        raise AuthenticationError('ORCID 登录入口无法识别。')
    href=link.get_attribute('href')
    u=urlsplit(href or '')
    if u.scheme!='https' or u.hostname!='idp-google-authenticator.nature.com' or u.path!='/orcid/pre-auth':
        raise AuthenticationError('ORCID 登录入口地址无法确认。')
    oauth_scopes=[]
    def capture_scope(request):
        url=urlsplit(request.url)
        if url.hostname=='orcid.org' and url.path=='/oauth/authorize':
            oauth_scopes.extend(parse_qs(url.query).get('scope',[]))
    page.on('request',capture_scope)
    page.goto(href,wait_until='domcontentloaded',timeout=45000)
    page.remove_listener('request',capture_scope)
    page.locator('#username-input').wait_for(timeout=30000)
    current=urlsplit(page.url)
    if current.scheme!='https' or current.hostname!='orcid.org':
        raise AuthenticationError('ORCID 官方登录地址无法确认。')
    page.locator('#username-input').fill(username)
    page.locator('#password').fill(password)
    # A redirect between filling and submission must never submit credentials elsewhere.
    if urlsplit(page.url).hostname!='orcid.org':
        raise AuthenticationError('ORCID 登录地址发生变化。')
    page.get_by_role('button',name=re.compile(r'Sign in to ORCID',re.I)).click()
    deadline=time.monotonic()+90
    consent_clicked=False
    while time.monotonic()<deadline:
        host=urlsplit(page.url).hostname or ''
        if host!='orcid.org':
            if not (host.endswith('.springernature.com') or host.endswith('.nature.com')):
                raise AuthenticationError('ORCID 返回的投稿平台地址无法确认。')
            if host=='submission.springernature.com':
                return
            page.wait_for_timeout(500)
            continue
        if page.locator('input[autocomplete="one-time-code"],input[name="otp"],input[name="verificationCode"]').count():
            raise AuthenticationError('ORCID 需要二次验证，请先在官方网页处理。')
        authorize=page.get_by_role('button',name=re.compile(r'^(Authorize access|Authorize|Allow access)$',re.I))
        if authorize.count() and authorize.first.is_visible():
            # Only the read scope requested by Springer's official link is supported.
            scopes=oauth_scopes or parse_qs(urlsplit(page.url).query).get('scope',[])
            if not scopes or any(scope not in ('/read-limited','/authenticate','openid') for scope in ' '.join(scopes).split()):
                raise AuthenticationError('ORCID 请求额外权限，请在官方网页确认。')
            if consent_clicked:
                raise AuthenticationError('ORCID 授权未完成，请在官方网页确认。')
            authorize.first.click();consent_clicked=True
        errors=page.locator('mat-error,[role="alert"]')
        if errors.count() and any(errors.nth(i).is_visible() for i in range(errors.count())):
            raise AuthenticationError('ORCID 登录未成功，请检查密码或官方页面提示。')
        page.wait_for_timeout(500)
    raise AuthenticationError('ORCID 已登录但未返回投稿系统，请在官方网页完成授权或账号关联。')


def fetch_orcid_result(account,network):
    from playwright.sync_api import sync_playwright,TimeoutError as BrowserTimeout,Error as BrowserError
    from journalcheck.sites.bmc import BMCChecker
    urls=account.get('submission_urls') or [account.get('submission_url','')]
    rows,results=[],[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True,proxy=browser_proxy(network),args=['--no-sandbox'])
        context=browser.new_context()
        page=context.new_page();page.set_default_timeout(15000)
        logged_in=False
        login_attempted=False
        try:
            for url in urls:
                try:
                    response=page.goto(url,wait_until='domcontentloaded',timeout=45000)
                    if page.locator('#login-via-orcid-url').count():
                        if login_attempted:
                            raise AuthenticationError('稿件链接需要重新登录或没有访问权限。')
                        login_attempted=True
                        login_orcid(page,account['username'],account['password'])
                        logged_in=True
                        response=page.goto(url,wait_until='domcontentloaded',timeout=45000)
                    if response and response.status in (403,404) or page.get_by_text('Not Found',exact=True).count():
                        results.append({'url':url,'ok':False,'category':'access','message':'当前 ORCID 没有该稿件的访问权限，或稿件链接已失效。'})
                        continue
                    page.locator('[data-current-step-description],[data-test="no-submissions"]').first.wait_for(timeout=20000)
                    checker=BMCChecker(url,account['username'],account['password'],site_name=account['name'],
                                       include_inactive=True,strict=True)
                    try:
                        found=checker.parse_page(SimpleNamespace(url=page.url,text=page.content()))
                    finally:
                        checker.session.close()
                    for row in found:
                        data=row.to_dict();data.setdefault('metadata',{})['tracking_url']=url;rows.append(data)
                    results.append({'url':url,'ok':True,'count':len(found)})
                except AuthenticationError as exc:
                    results.append({'url':url,'ok':False,'category':'authentication','message':str(exc)})
                except ParseError:
                    results.append({'url':url,'ok':False,'category':'parse','message':'稿件页面结构无法识别，保留旧状态。'})
                except BrowserTimeout:
                    results.append({'url':url,'ok':False,'category':'network','message':'ORCID 登录或稿件加载超时；请检查官方网页、授权和代理。'})
                except BrowserError:
                    results.append({'url':url,'ok':False,'category':'network','message':'ORCID 浏览器连接未完成，保留旧状态。'})
        finally:
            context.close();browser.close()
    return {'ok':True,'rows':rows,'targets':results,'partial':not all(r['ok'] for r in results),'login_confirmed':logged_in}


def fetch_orcid(account,network):
    result=fetch_orcid_result(account,network)
    if result['partial']:
        raise AuthenticationError('部分 ORCID 稿件未能完成检查。')
    return result['rows']
