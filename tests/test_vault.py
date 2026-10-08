import json
import time

import pytest

from tests.test_web import web, login, change_initial_password, protected_headers, OWNER
from tests.test_store import make_account, submission
from tests.test_portal_auth import gateway, b64url, OWNER as GW_OWNER
from journalcheck.server.app import _gateway_csrf


def ready(web):
    client, store, password, _ = web
    password = change_initial_password(client, password)
    csrf = login(client, password)
    account = make_account(store)
    return client, store, password, protected_headers(csrf), account


def test_reveal_is_explicit_audited_and_never_in_list_or_exports(web):
    client, store, password, headers, account = ready(web)
    secret = store.account(account['id'], private=True)['password']
    url = '/journal/api/vault/accounts/' + account['id'] + '/reveal'
    assert client.post(url, json={}, headers=headers).status_code == 403
    assert client.get(url, headers=OWNER).status_code == 405
    assert client.post('/journal/api/vault/unlock', json={'password':'wrong'}, headers=headers).status_code == 403
    assert client.post('/journal/api/vault/unlock', json={'password':password}, headers=headers).json()['unlocked']
    assert client.post(url, json={}, headers=OWNER).status_code == 403
    for purpose in ('view','copy'):
        response = client.post(url, json={'purpose':purpose}, headers=headers)
        assert response.json()['password'] == secret
        assert 0 < response.json()['visible_until'] - time.time() <= 30
        assert response.headers['cache-control'] == 'no-store'
    for path in ('accounts','accounts/'+account['id'],'vault/status','vault/audit','exports/current.csv','exports/history.csv'):
        assert secret not in client.get('/journal/api/'+path, headers=OWNER).text
    with store.db() as db:
        audit = [dict(r) for r in db.execute("SELECT * FROM audit WHERE action LIKE 'vault_%'")]
    assert [r['action'] for r in audit] == ['vault_unlock','vault_view','vault_copy']
    assert secret not in json.dumps(audit) and password not in json.dumps(audit)
    assert secret.encode() not in store.path.read_bytes()


def test_expiry_lock_session_binding_logout_and_password_change(web):
    client, store, password, headers, account = ready(web)
    url = '/journal/api/vault/accounts/' + account['id'] + '/reveal'
    def unlock():
        assert client.post('/journal/api/vault/unlock', json={'password':password}, headers=headers).status_code == 200
    unlock()
    with store.db() as db: db.execute('UPDATE vault_unlocks SET expires_at=?', (int(time.time())-1,))
    assert client.post(url, json={}, headers=headers).status_code == 403
    unlock()
    assert client.post('/journal/api/vault/lock', json={}, headers=headers).status_code == 200
    assert client.post(url, json={}, headers=headers).status_code == 403
    unlock()
    # Logging in creates a different session; the old grant cannot transfer.
    headers = protected_headers(login(client,password))
    assert client.post(url,json={},headers=headers).status_code == 403
    unlock()
    assert client.post('/journal/api/password', json={'old_password':password,'new_password':'another-long-synthetic-password'}, headers=headers).status_code == 200
    assert client.post(url,json={},headers=headers).status_code == 401
    headers = protected_headers(login(client,'another-long-synthetic-password'))
    assert client.post(url,json={},headers=headers).status_code == 403
    assert client.post('/journal/api/logout',json={},headers=headers).status_code == 200
    assert client.get('/journal/api/vault/status',headers=OWNER).status_code == 401


def test_archived_credentials_available_deleted_credentials_gone_and_edit_sync(web):
    client, store, password, headers, account = ready(web)
    store.apply_rows(account,[submission()],store.settings(),verified=True)
    assert client.post('/journal/api/vault/unlock',json={'password':password},headers=headers).status_code == 200
    url='/journal/api/vault/accounts/'+account['id']+'/reveal'
    update=client.patch('/journal/api/accounts/'+account['id'],json={'password':'updated-journal-secret'},headers=headers)
    assert update.status_code==200 and not update.json()['enabled']
    assert client.post(url,json={},headers=headers).json()['password']=='updated-journal-secret'
    client.post('/journal/api/accounts/'+account['id']+'/archive',json={},headers=headers)
    assert client.post(url,json={},headers=headers).status_code==200
    client.delete('/journal/api/accounts/'+account['id'],headers=headers)
    assert client.post(url,json={},headers=headers).status_code==404
    assert len(store.submissions(True))==1


def test_secondary_verification_rate_limit_and_bad_origin(web):
    client, _, password, headers, _ = ready(web)
    route='/journal/api/vault/unlock'
    assert client.post(route,json={'password':password},headers={**headers,'Origin':'https://evil.test'}).status_code==403
    for _ in range(10):assert client.post(route,json={'password':'incorrect'},headers=headers).status_code==403
    assert client.post(route,json={'password':password},headers=headers).status_code==429


def test_gateway_requires_signed_stepup_and_ignores_forged_browser_headers(gateway):
    import hashlib,hmac
    client, app, key, original, _ = gateway
    account=make_account(app.state.store)
    url='/journal/api/vault/accounts/'+account['id']+'/reveal'
    headers={**original,'Origin':'https://journal.example.test','X-CSRF-Token':_gateway_csrf(key,'sess-1'),'X-Vault-Unlocked':'true'}
    assert client.post(url,json={},headers=headers).status_code==403
    def context(until):
        body=b64url(json.dumps({'sub':GW_OWNER,'sid':'sess-1','iat':int(time.time()),'exp':int(time.time())+120,'vault_until':until}).encode())
        msg='gw1.'+body
        return {**headers,'X-Journal-Gateway':msg+'.'+b64url(hmac.new(key,msg.encode(),hashlib.sha256).digest())}
    assert client.post(url,json={},headers=context(int(time.time())+300)).status_code==200
    assert client.post(url,json={},headers=context(int(time.time())-1)).status_code==403
    assert client.post(url,json={},headers=context(int(time.time())+301)).status_code==403
    assert client.post('/journal/api/vault/unlock',json={'password':'irrelevant'},headers=headers).status_code==404
