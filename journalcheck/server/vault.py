"""Explicit, session-bound secret reveal. No secrets in list, audit or GET APIs."""
import time

from fastapi import HTTPException, Request

from .security import verify_password
from .store import now


def install_vault(app, store, session, gateway_identity, json_object):
    def expiry(request):
        if app.state.gateway_mode:
            context = gateway_identity(request)
            until = (context or {}).get('vault_until', 0)
            if (type(until) is not int or not context or type(context.get('iat')) is not int
                    or until > context['iat'] + 300):
                return 0
            return until
        current = session(request)
        if not current:
            return 0
        with store.db() as db:
            row = db.execute('SELECT expires_at FROM vault_unlocks WHERE sid=?', (current['token_hash'],)).fetchone()
        return row[0] if row else 0

    @app.get('/api/vault/status')
    def vault_status(request: Request):
        until = expiry(request)
        return {'unlocked':until > time.time(), 'expires_at':until, 'gateway':app.state.gateway_mode}

    @app.post('/api/vault/{action}')
    async def vault_action(action: str, request: Request):
        if action not in ('unlock', 'lock') or app.state.gateway_mode:
            raise HTTPException(404, '门户模式请通过门户验证接口解锁。')
        current = session(request)
        if action == 'unlock':
            cutoff = time.monotonic() - 300
            app.state.login_failures = [v for v in app.state.login_failures if v > cutoff]
            if len(app.state.login_failures) >= 10:
                raise HTTPException(429, '验证尝试过多，请五分钟后重试。')
            data = await json_object(request)
            password = data.get('password')
            if not isinstance(password, str) or len(password) > 512 or not verify_password(password, store.meta('admin_password')):
                app.state.login_failures.append(time.monotonic())
                raise HTTPException(403, '管理密码不正确。')
            app.state.login_failures.clear()
        until = int(time.time()) + 300 if action == 'unlock' else 0
        with store.db(True) as db:
            if not current or not db.execute('SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?',
                                             (current['token_hash'], now())).fetchone():
                raise HTTPException(401, '请重新登录。')
            db.execute('DELETE FROM vault_unlocks WHERE expires_at<=?', (int(time.time()),))
            db.execute('INSERT OR REPLACE INTO vault_unlocks VALUES (?,?)', (current['token_hash'], until))
            store.audit(db, 'vault_' + action)
        return {'unlocked':until > time.time(), 'expires_at':until}

    @app.post('/api/vault/accounts/{identity}/reveal')
    async def reveal(identity: str, request: Request):
        until = expiry(request)
        if until <= time.time():
            raise HTTPException(403, '密码库已锁定，请重新解锁。')
        data = await json_object(request)
        purpose = data.get('purpose', 'view')
        if purpose not in ('view', 'copy'):
            raise HTTPException(422, '操作无效。')
        with store.db(True) as db:
            row = db.execute('SELECT config FROM accounts WHERE id=? AND deleted=0', (identity,)).fetchone()
            if not row:
                raise HTTPException(404, '账号不存在或已删除。')
            password = store.unpack(row['config']).get('password', '')
            store.audit(db, 'vault_' + purpose, identity)
        return {'password':password, 'visible_until':min(until, time.time() + 30)}

    @app.get('/api/vault/audit')
    def audit():
        with store.db() as db:
            return {'items':[dict(row) for row in db.execute(
                "SELECT x.created_at,x.action,a.name account_name FROM audit x LEFT JOIN accounts a ON a.id=x.target "
                "WHERE x.action IN ('vault_view','vault_copy') ORDER BY x.created_at DESC LIMIT 50")]}
