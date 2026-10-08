import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import hashlib
import socket
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect
from playwright.sync_api import BrowserType
from playwright.sync_api import sync_playwright

from journalcheck.server.browser_profiles import BrowserBusyError, profile_lock, profile_path, tools_ready
from journalcheck.server.browser_sessions import BrowserSessions, read_json, run_session, write_json
from journalcheck.server.journal_names import scholarone_name
from journalcheck.server.scholarone import parse_author_page, fetch_scholarone
from journalcheck.server.store import Store
from journalcheck.server.app import create_app, COOKIE
from journalcheck.server.worker import Worker
from tests.test_web import web, OWNER, ORIGIN, login, protected_headers, change_initial_password
from tests.test_portal_auth import gateway

URL = 'https://mc.manuscriptcentral.com/ageing'
TOOLS = Path(__file__).resolve().parents[1] / '.runtime/browser-tools'
FIXTURE = (Path(__file__).parent / 'fixtures/scholarone_queue.html').read_text()


def account(store):
    return store.save_account({'name':'Synthetic journal account', 'platform':'scholarone',
        'base_url':URL, 'username':'synthetic@example.test', 'password':'synthetic-session-password'})


def test_automatic_worker_session_is_visible_without_a_manual_window(tmp_path):
    from journalcheck.server.security import private_write
    store = Store(tmp_path)
    saved = account(store)
    manager = BrowserSessions(store)
    assert manager.current(saved['id'])['status'] == 'idle'
    profile = profile_path(tmp_path, store.account(saved['id'], private=True))
    private_write(profile / 'state.fernet', store.pack({'cookies':[], 'origins':[]}).encode())
    write_json(profile / 'session.json', {'saved_at':'2026-10-08T00:00:00+00:00'})
    assert manager.current(saved['id'])['status'] == 'saved'
    store.save_account({'password':'changed-synthetic-password'}, saved['id'])
    assert manager.current(saved['id'])['status'] == 'idle'


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('Synthetic session did not reach the expected state')


def test_profiles_isolate_accounts_and_credentials_while_names_preserve_session(tmp_path):
    store=Store(tmp_path); first=account(store)
    private=store.account(first['id'],private=True)
    path=profile_path(tmp_path,private)
    store.save_account({'name':'Renamed journal account'},first['id'])
    assert profile_path(tmp_path,store.account(first['id'],private=True))==path
    store.save_account({'password':'different-synthetic-password'},first['id'])
    assert profile_path(tmp_path,store.account(first['id'],private=True))!=path
    other=account(store)
    assert profile_path(tmp_path,store.account(other['id'],private=True))!=path
    with profile_lock(path):
        with pytest.raises(BrowserBusyError):
            with profile_lock(path):
                pass
    assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('observed', ['','AGEING','Author Center','ScholarOne Manuscripts'])
def test_verified_journal_name_replaces_codes_and_generic_page_names(observed):
    assert scholarone_name('ageing',observed)=='Age and Ageing'
    html=FIXTURE.replace('Example Journal of Ageing',observed)
    assert parse_author_page(html,URL)[0]['source']=='Age and Ageing'


def test_manual_login_busy_does_not_increment_failures_or_send_alerts(tmp_path):
    store=Store(tmp_path); a=account(store)
    store.apply_rows(a,parse_author_page(FIXTURE,URL),store.settings(),verified=True)
    before=store.account(a['id'])
    store.queue_job(account_id=a['id'])
    assert Worker(store,fetcher=lambda *args:{'ok':False,'category':'busy'}).run_job()
    after=store.account(a['id'])
    assert after['failures']==before['failures']==0
    assert after['last_success_at']==before['last_success_at']
    assert after['last_error']==before['last_error']
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]==0


def test_browser_api_requires_auth_origin_csrf_and_never_returns_secrets(web,monkeypatch):
    client,store,password,_=web; a=account(store)
    path=f"/journal/api/accounts/{a['id']}/browser-session"
    assert client.get(path,headers=OWNER).status_code==401
    password=change_initial_password(client,password);csrf=login(client,password)
    assert client.post(path,headers=OWNER|ORIGIN).status_code==403
    assert client.post(path,headers={**protected_headers(csrf),'Origin':'https://evil.test'}).status_code==403
    assert client.get(path,headers=OWNER).json()['status']=='idle'
    manager=client.app.state.browsers
    monkeypatch.setattr(manager,'start',lambda identity:{'id':'a'*32,'account_id':identity,
        'status':'starting','url':'/journal/browser-login/'+'a'*32})
    response=client.post(path,headers=protected_headers(csrf))
    assert response.status_code==200
    assert not any(word in response.text for word in ('synthetic-session-password','vnc.sock','browser-profiles'))
    em=store.save_account({'name':'EM','platform':'em','base_url':'https://www.editorialmanager.com/test',
        'username':'synthetic','password':'synthetic-password'})
    assert client.get(f"/journal/api/accounts/{em['id']}/browser-session",headers=OWNER).status_code==422


def test_websocket_refuses_missing_identity_missing_session_and_foreign_origin(web):
    client,_,password,_=web
    url='/journal/browser-sessions/'+'a'*32+'/socket'
    for headers in ({},OWNER|ORIGIN):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url,headers=headers):
                pass
    password=change_initial_password(client,password);login(client,password)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url,headers={**OWNER,'Origin':'https://evil.test'}):
            pass


def test_gateway_websocket_requires_signed_bound_context(gateway):
    client,_,_,_,_=gateway
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/journal/browser-sessions/'+'a'*32+'/socket',
                headers=OWNER|ORIGIN):
            pass


def test_authenticated_websocket_forwards_only_fixed_private_socket(web,monkeypatch):
    client,_,password,_=web
    password=change_initial_password(client,password);login(client,password)
    manager=client.app.state.browsers
    monkeypatch.setattr(manager,'socket',lambda identity:Path('/private/test-vnc.sock'))
    monkeypatch.setattr(manager,'state',lambda identity:{'status':'active'})
    class Reader:
        sent=False
        async def read(self,size):
            if not self.sent:
                self.sent=True
                return b'RFB 003.008\n'
            await asyncio.sleep(30)
    class Writer:
        def write(self,data):
            pass
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            pass
    connect=AsyncMock(return_value=(Reader(),Writer()))
    monkeypatch.setattr('asyncio.open_unix_connection',connect)
    with client.websocket_connect('wss://testserver/journal/browser-sessions/'+'a'*32+'/socket',headers=OWNER|ORIGIN) as ws:
        assert ws.receive_bytes()==b'RFB 003.008\n'
        ws.send_bytes(b'RFB 003.008\n')
        ws.close()
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    connect.assert_awaited_once_with('/private/test-vnc.sock')


@pytest.mark.skipif(not tools_ready(TOOLS.parent),reason='Private VNC components not installed')
def test_manual_visible_browser_saves_session_and_worker_reuses_it_offline(monkeypatch):
    # Real headed Chromium + VNC, synthetic intercepted official-origin pages.
    # A short private runtime keeps Unix socket paths below the OS length limit.
    monkeypatch.setenv('JOURNALCHECK_BROWSER_TOOLS',str(TOOLS))
    original=BrowserType.launch_persistent_context
    passwords=[];external=[]
    def launch(browser,*args,**kwargs):
        context=original(browser,*args,**kwargs)
        def handle(route):
            request=route.request
            if not request.url.startswith(URL):
                external.append(request.url);route.abort();return
            cookie=request.headers.get('cookie','')
            if request.method=='POST':
                passwords.append(request.post_data)
                route.fulfill(status=200,headers={'Set-Cookie':'jc_session=valid; Path=/; Secure; HttpOnly'},
                    content_type='text/html',body='<a href="?author">Author</a>')
            elif 'jc_session=valid' in cookie:
                route.fulfill(status=200,content_type='text/html',body=
                    '<a href="?author">Author</a><a href="?submitted">Submitted Manuscripts</a>'+FIXTURE)
            else:
                route.fulfill(status=200,content_type='text/html',body='''<form method="post">
                    <input name="USERID"><input name="PASSWORD" type="password"><button id="logInButton">Log In</button>
                    </form><script>setInterval(()=>{const f=document.forms[0];if(f.USERID.value&&f.PASSWORD.value)f.requestSubmit()},200)</script>''')
        context.route('**/*',handle)
        return context
    monkeypatch.setattr(BrowserType,'launch_persistent_context',launch)
    with tempfile.TemporaryDirectory(prefix='jc-session-') as runtime:
        store=Store(Path(runtime));a=account(store);identity='b'*32
        store.bootstrap('synthetic-browser@example.test');store.set_meta('must_change_password','0')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        store.set_meta('public_origin',origin)
        from journalcheck.server.store import later
        with store.db() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?)',
                (hashlib.sha256(b'synthetic-browser-token').hexdigest(),'synthetic-browser-csrf',later(60)))
        app=create_app(store.root);manager=app.state.browsers
        folder=manager.folder(identity);folder.mkdir(mode=0o700)
        from journalcheck.server.security import private_write
        from journalcheck.server.store import later
        write_json(folder/'state.json',{'id':identity,'account_id':a['id'],'status':'starting',
            'message':'Synthetic session','expires_at':later(60)})
        private_write(folder/'snapshot',store.pack({'account':store.account(a['id'],private=True),
            'settings':store.settings()}).encode())
        thread=threading.Thread(target=run_session,args=(runtime,identity),daemon=True);thread.start()
        class ThreadProcess:
            def poll(self):
                return None if thread.is_alive() else 0
        manager.processes[identity]=ThreadProcess()
        import uvicorn
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
        server_thread=threading.Thread(target=server.run,daemon=True);server_thread.start()
        try:
            wait_for(lambda:server.started)
            wait_for(lambda:read_json(folder/'state.json').get('status')=='active')
            assert (folder/'vnc.sock').stat().st_mode & 0o777 == 0o600
            assert not (folder/'snapshot').exists()
            # Actual noVNC module and authenticated WebSocket, not synthetic API replies.
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
                ui=browser.new_context(extra_http_headers={'Tailscale-User-Login':'synthetic-browser@example.test'})
                ui.add_cookies([{'name':COOKIE,'value':'synthetic-browser-token','url':origin}])
                page=ui.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(origin+'/journal/browser-login/'+identity)
                page.locator('#desktop canvas').wait_for(timeout=15000)
                wait_for(lambda:page.locator('#desktop canvas').evaluate('(e)=>e.width')==1280)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth')<=390
                page.locator('#fill').click()
                wait_for(lambda:len(passwords)==1)
                page.locator('#save:not([disabled])').wait_for()
                page.locator('#save').click()
                wait_for(lambda:'会话已保存' in page.locator('#status').inner_text())
                assert not errors
                browser.close()
            thread.join(timeout=5);assert not thread.is_alive()
            saved_profile=profile_path(store.root,store.account(a['id'],private=True))
            capsule=saved_profile/'state.fernet'
            assert b'jc_session' not in capsule.read_bytes()
            assert capsule.stat().st_mode & 0o777 == 0o600
            rows=fetch_scholarone(store.account(a['id'],private=True),{'_runtime_root':runtime})
            assert len(rows)==2
            assert len(passwords)==1, 'Saved cookie must avoid another credential submission'
            assert not external
            assert manager.current(a['id'])['status']=='saved'
            store.save_account({'username':'different-synthetic@example.test'},a['id'])
            assert manager.current(a['id'])['status']=='expired'
            profile=store.root/'browser-profiles'/a['id']
            assert profile.exists()
            store.delete_account(a['id']);manager.delete_account(a['id'])
            assert not profile.exists()
        finally:
            if thread.is_alive():
                manager.command(identity,'cancel');thread.join(timeout=10)
            server.should_exit=True;server_thread.join(timeout=10)
