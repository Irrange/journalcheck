from __future__ import annotations
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from email.header import decode_header
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .security import hash_password, verify_password
from .store import Store, now, later, uid, DEFAULTS

BASE = '/journal'
COOKIE = 'journalcheck_session'
GATEWAY_VERSION = 'gw1'
GATEWAY_SKEW = 5


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _gateway_csrf(key: bytes, sid: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(key, f'csrf1.{sid}'.encode(), hashlib.sha256).digest()).rstrip(b'=').decode()[:32]


def verify_gateway(key: bytes | None, token: str, expected_login: str, now_ts: int) -> dict | None:
    """Validate the HMAC-signed portal gateway context. Never trust the header alone."""
    if not key or not token:
        return None
    try:
        version, body, signature = token.split('.')
        if version != GATEWAY_VERSION:
            return None
        expected = hmac.new(key, f'{version}.{body}'.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url_decode(signature), expected):
            return None
        payload = json.loads(_b64url_decode(body))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        exp, iat = int(payload['exp']), int(payload['iat'])
    except (KeyError, TypeError, ValueError):
        return None
    if exp < now_ts - GATEWAY_SKEW or iat > now_ts + GATEWAY_SKEW:
        return None
    if not expected_login or not hmac.compare_digest(str(payload.get('sub', '')), expected_login):
        return None
    if not payload.get('sid'):
        return None
    return payload


def _load_gateway_key(path: str | None) -> bytes | None:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.exists():
        return None
    try:
        raw = candidate.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    try:
        key = _b64url_decode(raw)
    except (ValueError, TypeError):
        return None
    return key if len(key) >= 32 else None



class ExternalPrefixMiddleware:
    """Restore the external path after Serve strips it, including for mounted static apps."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            path = scope['path']
            if path != BASE and not path.startswith(BASE + '/'):
                scope = {**scope, 'path': BASE + path,
                         'raw_path': BASE.encode() + scope.get('raw_path', path.encode())}
        await self.app(scope, receive, send)


def create_app(root: Path | None = None) -> FastAPI:
    store = Store(root or Path(os.environ.get('JOURNALCHECK_RUNTIME','.runtime')))
    gateway_key = _load_gateway_key(os.environ.get('JOURNALCHECK_GATEWAY_KEY'))
    gateway_mode = os.environ.get('JOURNALCHECK_GATEWAY_MODE', '').lower() in ('1', 'true', 'yes')
    if gateway_mode and not gateway_key:
        raise RuntimeError('JOURNALCHECK_GATEWAY_MODE is enabled but JOURNALCHECK_GATEWAY_KEY is missing or invalid.')
    from .browser_sessions import BrowserSessions
    from .browser_profiles import tools_path
    from contextlib import asynccontextmanager
    browsers = BrowserSessions(store)
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            browsers.close()
    app = FastAPI(root_path=BASE, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.login_failures = []
    app.state.gateway_key = gateway_key
    app.state.gateway_mode = gateway_mode
    app.state.browsers = browsers
    directory = Path(__file__).parent
    templates = Jinja2Templates(directory=str(directory / 'templates'))
    app.mount('/static', StaticFiles(directory=directory / 'static'), name='static')
    novnc = tools_path(store.root) / 'novnc'
    if novnc.is_dir():
        app.mount('/browser-assets', StaticFiles(directory=novnc), name='browser-assets')

    def identity_check(request):
        raw = request.headers.get('tailscale-user-login','')
        try:
            login = ''.join(value.decode(encoding or 'utf-8') if isinstance(value,bytes) else value
                            for value,encoding in decode_header(raw))
        except (UnicodeError,LookupError):
            login = ''
        allowed = store.meta('allowed_login')
        return bool(allowed and secrets.compare_digest(login,allowed))

    def gateway_identity(request):
        """In explicit gateway mode, validate the signed portal context instead of Tailscale."""
        if not app.state.gateway_mode:
            return None
        token = request.headers.get('x-journal-gateway', '')
        payload = verify_gateway(app.state.gateway_key, token, store.meta('allowed_login', ''), int(time.time()))
        return payload

    def session(request):
        token = request.cookies.get(COOKIE,'')
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with store.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?',(hashed,now())).fetchone()
        return dict(row) if row else None

    def gateway_session(request):
        """A verified gateway request is an authenticated session for this mode."""
        payload = gateway_identity(request)
        if not payload:
            return None
        return {'token_hash': payload['sid'], 'csrf': _gateway_csrf(app.state.gateway_key, payload['sid']),
                'gateway': True, 'login': payload['sub']}

    @app.middleware('http')
    async def protect(request: Request, call_next):
        path = request.scope['path']
        if path.startswith(BASE + '/'):
            path = path[len(BASE):]
        if app.state.gateway_mode:
            # Fail closed: the external gateway must present a valid signed context.
            if not gateway_identity(request):
                return JSONResponse({'detail':'门户认证上下文缺失或无效。'},status_code=403)
        elif not identity_check(request):
            return JSONResponse({'detail':'仅允许已配置的 Tailscale 身份访问。'},status_code=403)
        public = path in ('/','/api/login','/api/session') or path.startswith('/static/')
        current = gateway_session(request) if app.state.gateway_mode else session(request)
        if not public and not current:
            return JSONResponse({'detail':'请先登录。'},status_code=401)
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin = request.headers.get('origin','')
            # Serve terminates TLS; only accept the canonical same-origin HTTPS URL.
            expected = store.meta('public_origin')
            if not expected or origin != expected:
                return JSONResponse({'detail':'请求来源不匹配。'},status_code=403)
            if path != '/api/login' and (not current or not secrets.compare_digest(
                    request.headers.get('x-csrf-token',''), current['csrf'])):
                return JSONResponse({'detail':'请求验证失败，请重新登录。'},status_code=403)
        if not app.state.gateway_mode and current and store.meta('must_change_password') == '1' \
                and path.startswith('/api/') and path not in (
                '/api/session','/api/password','/api/logout','/api/login'):
            return JSONResponse({'detail':'请先修改初始管理密码。'},status_code=403)
        try:
            length = int(request.headers.get('content-length','0'))
        except ValueError:
            return JSONResponse({'detail':'请求长度无效。'},status_code=400)
        if length > 131072:
            return JSONResponse({'detail':'请求过大。'},status_code=413)
        if request.method not in ('GET','HEAD','OPTIONS') and len(await request.body()) > 131072:
            return JSONResponse({'detail':'请求过大。'},status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({'detail':str(exc)},status_code=422)

    @app.exception_handler(KeyError)
    async def missing_record(request, exc):
        return JSONResponse({'detail':'记录不存在。'},status_code=404)

    async def json_object(request):
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise HTTPException(422, '请求须为有效的 JSON 对象。') from None
        if not isinstance(data, dict):
            raise HTTPException(422, '请求须为有效的 JSON 对象。')
        return data

    @app.get('/', response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request=request,name='index.html',
                                          context={'base_path':BASE,'gateway_mode':app.state.gateway_mode})

    @app.get('/api/session')
    def get_session(request: Request):
        current = gateway_session(request) if app.state.gateway_mode else session(request)
        payload = {'authenticated':bool(current),'csrf':current['csrf'] if current else None,
                   'must_change_password':(not app.state.gateway_mode) and store.meta('must_change_password') == '1'}
        if app.state.gateway_mode:
            # Only gateway mode advertises the extension, keeping legacy responses identical.
            payload['gateway'] = True
        return payload

    @app.post('/api/login')
    async def login(request: Request):
        if app.state.gateway_mode:
            raise HTTPException(404,'网关模式下请在门户登录。')
        data = await json_object(request)
        cutoff = time.monotonic()-300
        failures = [v for v in app.state.login_failures if v > cutoff]
        app.state.login_failures = failures
        if len(failures) >= 10:
            raise HTTPException(429,'登录尝试过多，请五分钟后重试。')
        password = data.get('password','')
        if not isinstance(password,str) or len(password)>512 or not verify_password(password,store.meta('admin_password','')):
            failures.append(time.monotonic())
            raise HTTPException(401,'管理密码不正确。')
        app.state.login_failures.clear()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE expires_at<=?',(now(),))
            old_token = request.cookies.get(COOKIE)
            if old_token:
                db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(old_token.encode()).hexdigest(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,later(86400)))
            store.audit(db,'login')
        response = JSONResponse({'authenticated':True,'csrf':csrf,'must_change_password':store.meta('must_change_password')=='1','gateway':False})
        response.set_cookie(COOKIE,token,max_age=86400,secure=True,httponly=True,samesite='lax',path=BASE+'/')
        return response

    @app.post('/api/logout')
    def logout(request: Request):
        if app.state.gateway_mode:
            # Logout is central in gateway mode; the browser performs it same-origin.
            return JSONResponse({'ok':True,'gateway_logout':'/portal-auth/logout'})
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?',(session(request)['token_hash'],))
        response = JSONResponse({'ok':True})
        response.delete_cookie(COOKIE,path=BASE+'/',secure=True,httponly=True,samesite='lax')
        return response

    @app.post('/api/password')
    async def password(request: Request):
        if app.state.gateway_mode:
            raise HTTPException(404,'网关模式下请在门户修改密码。')
        data = await json_object(request)
        new = data.get('new_password','')
        old = data.get('old_password','')
        if not isinstance(new,str) or not 12<=len(new)<=512:
            raise ValueError('新管理密码须为 12 至 512 个字符。')
        if not verify_password(old,store.meta('admin_password')):
            raise HTTPException(401,'原管理密码不正确。')
        if new == old:
            raise ValueError('请设置一个不同于原密码的新管理密码。')
        with store.db() as db:
            db.execute("UPDATE meta SET value=? WHERE key='admin_password'",(hash_password(new),))
            db.execute("UPDATE meta SET value='0' WHERE key='must_change_password'")
            db.execute('DELETE FROM sessions')
            store.audit(db,'password_changed')
        (store.root/'initial-password.txt').unlink(missing_ok=True)
        response = JSONResponse({'ok':True})
        response.delete_cookie(COOKIE,path=BASE+'/',secure=True,httponly=True,samesite='lax')
        return response

    def required_account(identity):
        account = store.account(identity)
        if not account:
            raise KeyError(identity)
        return account

    def validate_account(data):
        for key in ('name','platform','username'):
            if not isinstance(data.get(key),str) or not data[key].strip() or len(data[key])>500:
                raise ValueError('请填写有效的账号名称、平台和用户名。')
            data[key] = data[key].strip()
        platform = data['platform']
        domains = {'aha':('aha-journals.org','ejournalpress.com'), 'em':('editorialmanager.com',),
                   'bmc':('submission.springernature.com',), 'scholarone':('mc.manuscriptcentral.com',)}
        if platform not in domains:
            raise ValueError('请选择 AHA、EM、BMC 或 ScholarOne。')
        if platform=='bmc':
            method=data.get('login_method','password')
            if method not in ('password','orcid'):
                raise ValueError('请选择邮箱密码或 ORCID 登录。')
            data['login_method']=method
            if method=='orcid' and not re.fullmatch(r'\d{4}-\d{4}-\d{4}-\d{3}[\dX]',data['username']):
                raise ValueError('请填写格式为 0000-0000-0000-0000 的 ORCID。')
            urls = data.get('submission_urls')
            if urls is None:
                urls = [data['submission_url']] if data.get('submission_url') else []
            if not isinstance(urls, list) or len(urls) > 30:
                raise ValueError('每个账号最多追踪 30 篇文章。')
            from .account_urls import bmc_submission_url
            data['submission_urls'] = list(dict.fromkeys(bmc_submission_url(item) for item in urls))
            data['submission_url'] = next(iter(data['submission_urls']), '')
        else:
            try:
                url = urlsplit(data.get('base_url', ''))
                host = (url.hostname or '').lower()
            except ValueError:
                raise ValueError('期刊地址无效。')
            if url.scheme != 'https' or url.username or url.password or not any(host == d or host.endswith('.' + d) for d in domains[platform]):
                raise ValueError('请填写所选投稿平台的 HTTPS 官方地址。')
            if platform == 'em':
                from .account_urls import editorial_manager_url
                data['base_url'], data['journal_code'] = editorial_manager_url(data['base_url'])
            elif platform == 'scholarone':
                from .account_urls import scholarone_url
                data['base_url'], data['journal_code'] = scholarone_url(data['base_url'])
        if not isinstance(data.get('journal_name', ''), str) or len(data.get('journal_name', '')) > 120:
            raise ValueError('期刊显示名称最多 120 字。')
        data['journal_name'] = data.get('journal_name', '').strip()
        if not isinstance(data.get('password',''),str) or len(data.get('password',''))>2000:
            raise ValueError('账号密码无效。')
        return data

    @app.get('/api/accounts')
    def accounts():
        return {'items':store.accounts()}

    @app.post('/api/accounts')
    async def create_account(request: Request):
        return store.save_account(validate_account(await json_object(request)))

    @app.put('/api/accounts/{identity}')
    async def update_account(identity: str,request: Request):
        return store.save_account(validate_account(await json_object(request)),identity)

    @app.get('/api/accounts/{identity}')
    def account_detail(identity: str):
        return store.account_detail(identity)

    @app.patch('/api/accounts/{identity}')
    async def patch_account(identity: str, request: Request):
        old = required_account(identity)
        data = await json_object(request)
        allowed = {'name','journal_name','username','password','login_method','base_url'}
        if not isinstance(data, dict) or not data or set(data) - allowed:
            raise ValueError('请仅修改账号资料；文章链接请通过文章管理修改。')
        checked = validate_account({**old, **data})
        # Keep link fields out of the write: save_account merges current targets under its transaction.
        return store.save_account({key: checked[key] for key in data}, identity)

    @app.post('/api/accounts/{identity}/targets')
    async def add_targets(identity: str, request: Request):
        data = await json_object(request)
        return store.add_targets(identity, data.get('urls') if isinstance(data, dict) else None)

    @app.patch('/api/accounts/{identity}/targets/{target_id}')
    async def replace_target(identity: str, target_id: str, request: Request):
        data = await json_object(request)
        return store.replace_target(identity, target_id, data.get('url') if isinstance(data, dict) else None)

    @app.delete('/api/accounts/{identity}/targets/{target_id}')
    def remove_target(identity: str, target_id: str):
        return {'ok': True, 'account': store.remove_target(identity, target_id)}

    @app.delete('/api/accounts/{identity}')
    def delete_account(identity: str):
        store.delete_account(identity)
        browsers.delete_account(identity)
        return {'ok':True}

    @app.get('/api/accounts/{identity}/browser-session')
    def current_browser_session(identity: str):
        return browsers.current(identity)

    @app.post('/api/accounts/{identity}/browser-session')
    def start_browser_session(identity: str):
        from .browser_profiles import BrowserBusyError
        try:
            return browsers.start(identity)
        except BrowserBusyError:
            raise HTTPException(409, '此账号有后台检查正在运行，请稍后重新打开登录入口。') from None

    @app.get('/api/browser-sessions/{identity}')
    def browser_session(identity: str):
        return browsers.state(identity)

    @app.post('/api/browser-sessions/{identity}/{action}')
    def browser_command(identity: str, action: str):
        browsers.state(identity)
        return browsers.command(identity, action)

    @app.get('/browser-login/{identity}', response_class=HTMLResponse)
    def browser_login(identity: str, request: Request):
        state = browsers.state(identity)
        account = required_account(state['account_id'])
        return templates.TemplateResponse(request=request, name='browser-login.html',
            context={'base_path':BASE, 'browser_id':identity, 'account_id':account['id'],
                     'journal_name':account.get('journal_name') or account['name']})

    @app.websocket('/browser-sessions/{identity}/socket')
    async def browser_socket(websocket: WebSocket, identity: str):
        import asyncio
        current = gateway_session(websocket) if app.state.gateway_mode else session(websocket)
        authorised = bool(current and (app.state.gateway_mode or identity_check(websocket)))
        if (not authorised or websocket.headers.get('origin') != store.meta('public_origin')
                or not app.state.gateway_mode and store.meta('must_change_password') == '1'):
            await websocket.close(code=1008)
            return
        try:
            socket_path = browsers.socket(identity)
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
        except (KeyError, ValueError, OSError):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        async def to_browser():
            while True:
                data = await websocket.receive_bytes()
                if len(data) > 1048576:
                    raise ValueError('Frame too large')
                writer.write(data)
                await writer.drain()
        async def to_client():
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                await websocket.send_bytes(data)
        async def monitor():
            # Re-check cookie sessions and state even while the desktop is idle.
            # Gateway contexts expire quickly. Reconnects pass through forward_auth
            # again, so logout or a password change stops further desktop access.
            while True:
                await asyncio.sleep(2)
                if browsers.state(identity)['status'] not in ('starting','active'):
                    return
                if not app.state.gateway_mode and not session(websocket):
                    return
                if app.state.gateway_mode and not gateway_session(websocket):
                    return
        tasks = [asyncio.create_task(fn()) for fn in (to_browser, to_client, monitor)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            try:
                await websocket.close()
            except (WebSocketDisconnect, RuntimeError):
                pass

    @app.post('/api/accounts/{identity}/{action}')
    def action_account(identity: str,action: str):
        account = required_account(identity)
        if action in ('verify','enable'):
            if account['archived']:
                raise ValueError('账号已归档，请先恢复账号。')
            if account['platform'] == 'bmc' and not account['submission_urls']:
                raise ValueError('请先添加至少一篇文章，再验证并启用。')
            return store.queue_job('verify',identity)
        if action not in ('pause','archive','restore'):
            raise HTTPException(404,'操作不存在。')
        with store.db() as db:
            db.execute('UPDATE accounts SET enabled=0,revision=revision+1,archived=CASE WHEN ? THEN 1 WHEN ? THEN 0 ELSE archived END WHERE id=?',
                       (action=='archive',action=='restore',identity))
            store.audit(db,'account_'+action,identity)
        return store.account(identity)

    @app.get('/api/submissions')
    def submissions(archived: bool=False):
        return {'items':store.submissions(archived)}

    @app.get('/api/submissions/{identity}')
    def submission(identity: str):
        result = store.submission(identity)
        if result is None:
            raise KeyError(identity)
        return result

    @app.post('/api/submissions/{identity}/{action}')
    def action_submission(identity: str,action: str):
        if not store.submission(identity):
            raise KeyError(identity)
        if action not in ('archive','restore'):
            raise HTTPException(404,'操作不存在。')
        with store.db() as db:
            db.execute('UPDATE submissions SET archived=?,manual_archived=? WHERE id=?',(action=='archive',action=='archive',identity))
            store.audit(db,'submission_'+action,identity)
        return {'ok':True}

    @app.post('/api/refresh')
    async def refresh(request: Request):
        data = await json_object(request)
        identity = data.get('account_id')
        if identity:
            account = required_account(identity)
            if account['platform'] == 'bmc' and not account['submission_urls']:
                raise ValueError('请先添加至少一篇文章，再验证并启用。')
            if not account['enabled'] or account['archived']:
                raise ValueError('请先验证并启用该账号。')
        elif not any(a['enabled'] and not a['archived'] for a in store.accounts()):
            raise ValueError('请先添加并验证期刊账号。')
        return store.queue_job('refresh',identity)

    @app.get('/api/jobs')
    def jobs():
        with store.db() as db:
            items = [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 100')]
        for item in items:
            item['results'] = json.loads(item['results'])
        return {'items':items}

    @app.get('/api/settings')
    def settings():
        return store.public_settings()

    @app.put('/api/settings')
    async def update_settings(request: Request):
        data = await json_object(request)
        if data.get('interval_minutes',60) not in (30,60,120,360):
            raise ValueError('检查周期请选择 30、60、120 或 360 分钟。')
        if any(key not in DEFAULTS for key in data):
            raise ValueError('配置字段无效；通知仅支持 PushPlus 邮件。')
        for key in ('auto_refresh','pushplus_enabled'):
            if key in data and not isinstance(data[key],bool):
                raise ValueError('开关字段须为布尔值。')
        for key in ('pushplus_token','pushplus_option','no_proxy','http_proxy','https_proxy'):
            if key in data and (not isinstance(data[key],str) or len(data[key])>4000 or '\n' in data[key] or '\r' in data[key]):
                raise ValueError('配置字段格式无效。')
        if data.get('pushplus_token') and not re.fullmatch(r'[A-Za-z0-9_-]{16,256}',data['pushplus_token']):
            raise ValueError('PushPlus Token 格式无效。')
        if data.get('pushplus_option') and not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',data['pushplus_option']):
            raise ValueError('邮件编码格式无效；使用官方邮件渠道时请留空。')
        for key in ('http_proxy','https_proxy'):
            if data.get(key):
                u = urlsplit(data[key])
                if u.scheme not in ('http','https') or not u.hostname:
                    raise ValueError('代理须为有效 HTTP/HTTPS URL。')
            elif key in data and store.public_settings().get(key+'_configured'):
                data.pop(key)
        return store.update_settings(data)

    @app.get('/api/notifications')
    def notifications():
        with store.db() as db:
            items = [dict(r) for r in db.execute('SELECT * FROM notifications ORDER BY created_at DESC,rowid DESC LIMIT 100')]
        for item in items:
            item['payload'] = json.loads(item['payload'])
        return {'items':items}

    @app.post('/api/notifications/test')
    async def test_notification(request: Request):
        data = await json_object(request)
        channel = data.get('channel')
        if channel != 'pushplus':
            raise ValueError('请选择 PushPlus 邮件通知。')
        with store.db() as db:
            channels = store.enqueue_notification(db,uid(),{'subject':'journalcheck 测试通知','body':'网页通知配置测试：'+now()},only=channel)
            if not channels:
                raise ValueError('请先保存并启用该通知渠道的完整配置。')
            store.audit(db,'notification_test',channel)
        return {'ok':True}

    @app.post('/api/notifications/{identity}/retry')
    def retry_notification(identity: str):
        with store.db() as db:
            row = db.execute('SELECT * FROM notifications WHERE id=?',(identity,)).fetchone()
            if not row:
                raise KeyError(identity)
            if row['channel']!='pushplus':
                raise ValueError('旧通知渠道已停用；历史记录不可补发。')
            if row['status']!='failed':
                raise ValueError('仅失败通知可以补发；已受理的通知请在 PushPlus 消息列表确认。')
            db.execute("UPDATE notifications SET status='pending',attempts=0,next_attempt_at=?,last_error=NULL WHERE id=?",(now(),identity))
            store.audit(db,'notification_retry',identity)
        return {'ok':True}

    @app.get('/api/health')
    def health():
        seen = store.meta('worker_seen_at')
        healthy = bool(seen and (datetime.now(timezone.utc)-datetime.fromisoformat(seen)).total_seconds()<90)
        return {'web':'ok','worker':'ok' if healthy else 'stale','worker_seen_at':seen,
                'last_backup_at':store.meta('last_backup_at'),
                'accounts_enabled':sum(bool(a['enabled'] and not a['archived']) for a in store.accounts()),
                'submissions_active':len(store.submissions())}

    @app.get('/api/exports/{kind}.csv')
    def export(kind: str):
        output = io.StringIO()
        if kind=='current':
            keys = ['account_name','platform','manuscript_number','title','status','status_date','last_success_at','missing','archived']
            rows = store.submissions(False)+store.submissions(True)
        elif kind=='history':
            keys = ['id','submission_id','created_at','kind','payload']
            with store.db() as db:
                rows = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY created_at,rowid')]
        else:
            raise HTTPException(404,'导出类型不存在。')
        writer = csv.DictWriter(output,fieldnames=keys,extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            # Prevent spreadsheet formula injection in exported third-party content.
            writer.writerow({k:("'"+str(row.get(k,'')) if str(row.get(k,'')).startswith(('=','+','-','@','\t','\r')) else row.get(k,'')) for k in keys})
        return Response('\ufeff'+output.getvalue(),media_type='text/csv',
                        headers={'Content-Disposition':f'attachment; filename="journalcheck-{kind}.csv"'})
    app.add_middleware(ExternalPrefixMiddleware)
    return app
