import pytest
from fastapi.testclient import TestClient

from journalcheck.server.app import BASE, COOKIE, create_app


OWNER = {"Tailscale-User-Login": "owner@example.test"}
ORIGIN = {"Origin": "https://testserver"}


@pytest.fixture
def web(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    assert store.bootstrap("owner@example.test") is True
    store.set_meta("public_origin", "https://testserver")
    initial_password = (tmp_path / "initial-password.txt").read_text(encoding="utf-8").strip()
    client = TestClient(app, base_url="https://testserver", root_path=BASE)
    try:
        yield client, store, initial_password, tmp_path
    finally:
        client.close()


def login(client, password, headers=None):
    response = client.post("/journal/api/login", json={"password": password}, headers={**OWNER, **ORIGIN, **(headers or {})})
    assert response.status_code == 200
    return response.json()["csrf"]


def protected_headers(csrf):
    return {**OWNER, **ORIGIN, "X-CSRF-Token": csrf}


@pytest.mark.parametrize('body', ['[]', 'null', '"string"', '{broken'])
def test_malformed_json_is_rejected_without_internal_error(web, body):
    client, _, password, _ = web
    password = change_initial_password(client, password)
    csrf = login(client, password)
    for method, path in [('POST', 'refresh'), ('POST', 'accounts'), ('PUT', 'settings'), ('POST', 'password')]:
        response = client.request(method, '/journal/api/' + path, content=body,
                                  headers={**protected_headers(csrf), 'Content-Type':'application/json'})
        assert response.status_code == 422


def test_invalid_password_type_returns_auth_error(web):
    client, _, password, _ = web
    csrf = login(client, password)
    response = client.post('/journal/api/password', headers=protected_headers(csrf),
                           json={'old_password':[], 'new_password':'synthetic-long-password'})
    assert response.status_code == 401


def change_initial_password(client, initial_password):
    csrf = login(client, initial_password)
    response = client.post(
        "/journal/api/password",
        json={"old_password": initial_password, "new_password": "changed-password-long-2026"},
        headers=protected_headers(csrf),
    )
    assert response.status_code == 200
    return "changed-password-long-2026"


def test_tailscale_identity_and_root_path_protect_actual_routes(web):
    client, _, _, _ = web

    assert client.get("/journal/api/session").status_code == 403
    assert client.get("/journal/api/session", headers=OWNER).status_code == 200
    assert client.get("/journal/api/session", headers=OWNER).json() == {
        "authenticated": False, "csrf": None, "must_change_password": True,
    }
    assert client.get("/journal/api/accounts", headers=OWNER).status_code == 401
    # ASGI root_path is /journal, while TestClient routes remain prefix-stripped.
    assert client.get("/journal/", headers=OWNER).status_code == 200


def test_login_origin_csrf_and_forced_initial_password_change(web):
    client, store, initial_password, root = web
    assert client.post("/journal/api/login", json={"password": initial_password}, headers=OWNER).status_code == 403
    assert client.post("/journal/api/login", json={"password": "wrong"}, headers={**OWNER, "Origin": "https://evil.test"}).status_code == 403
    assert client.post("/journal/api/login", json={"password": "wrong"}, headers={**OWNER, **ORIGIN}).status_code == 401

    csrf = login(client, initial_password)
    assert client.get("/journal/api/accounts", headers=OWNER).status_code == 403
    assert client.get("/journal/api/session", headers=OWNER).json()["must_change_password"] is True
    assert client.post("/journal/api/refresh", json={}, headers=ORIGIN | OWNER).status_code == 403
    assert client.post("/journal/api/password", json={
        "old_password": initial_password, "new_password": "changed-password-long-2026",
    }, headers={**OWNER, **ORIGIN, "X-CSRF-Token": "wrong-token"}).status_code == 403

    new_password = change_initial_password(client, initial_password)
    assert not (root / "initial-password.txt").exists()
    assert store.meta("must_change_password") == "0"
    assert client.get("/journal/api/session", headers=OWNER).json()["authenticated"] is False
    new_csrf = login(client, new_password)
    assert client.get("/journal/api/accounts", headers=OWNER).status_code == 200
    assert client.get("/journal/api/session", headers=OWNER).json()["must_change_password"] is False
    assert new_csrf


def test_logout_invalidates_session_and_cookie(web):
    client, _, initial_password, _ = web
    csrf = login(client, initial_password)
    assert COOKIE in client.cookies

    response = client.post("/journal/api/logout", json={}, headers=protected_headers(csrf))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert client.get("/journal/api/session", headers=OWNER).json()["authenticated"] is False
    assert client.get("/journal/api/accounts", headers=OWNER).status_code == 401


def test_settings_never_return_secret_values_and_refresh_requires_an_account(web):
    client, _, initial_password, _ = web
    password = change_initial_password(client, initial_password)
    csrf = login(client, password)
    headers = protected_headers(csrf)
    response = client.put('/journal/api/settings',json={'pushplus_token':'test-pushplus-token-secret','pushplus_option':'demo'},headers=headers)
    assert response.status_code==200
    public=client.get('/journal/api/settings',headers=OWNER)
    assert public.status_code==200
    data=public.json()
    assert 'pushplus_token' not in data
    assert data['pushplus_token_configured'] is True
    assert 'test-pushplus-token-secret' not in public.text
    assert not any(key.startswith('smtp') or key.startswith('wecom') for key in data)
    assert client.put('/journal/api/settings',json={'pushplus_token':''},headers=headers).status_code==200
    assert client.get('/journal/api/settings',headers=OWNER).json()['pushplus_token_configured'] is True
    assert client.put('/journal/api/settings',json={'smtp_host':'mail.test'},headers=headers).status_code==422
    assert client.put('/journal/api/settings',json={'pushplus_token':'bad token'},headers=headers).status_code==422

    refresh = client.post("/journal/api/refresh", json={}, headers=headers)
    assert refresh.status_code == 422
    assert "添加并验证" in refresh.json()["detail"]


def test_csv_export_escapes_formula_like_submission_content(web):
    client, store, initial_password, _ = web
    password = change_initial_password(client, initial_password)
    csrf = login(client, password)
    account = store.save_account({
        "name": "Example journal", "platform": "aha", "username": "author@example.test",
        "password": "journal-account-password", "base_url": "https://aha-journals.org/login",
    })
    store.apply_rows(account, [{
        "site": "aha", "source": "live", "manuscript_number": "JHA-1",
        "title": "=HYPERLINK(\"https://evil.test\",\"open\")", "status": "Under Review",
    }], store.settings(), verified=True)

    response = client.get("/journal/api/exports/current.csv", headers=OWNER)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK" in response.text
    assert "X-Content-Type-Options" in response.headers
    assert csrf


def test_account_save_has_no_login_side_effect_and_preserves_password_spaces(web):
    client,store,initial,_=web
    password=change_initial_password(client,initial)
    csrf=login(client,password)
    data={'name':'EM demo','platform':'em','username':'demo','password':'  significant spaces  ',
          'base_url':'https://www.editorialmanager.com/ghrpj/default2.aspx','submission_url':''}
    response=client.post('/journal/api/accounts',json=data,headers=protected_headers(csrf))
    assert response.status_code==200
    account=response.json()
    assert account['password_configured'] and 'password' not in account
    assert account['journal_code']=='ghrpj'
    assert account['enabled']==0 and account['last_attempt_at'] is None
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
    data['password']=''
    data['name']='Renamed'
    updated=client.put('/journal/api/accounts/'+account['id'],json=data,headers=protected_headers(csrf))
    assert updated.status_code==200
    assert store.account(account['id'],True)['password']=='  significant spaces  '
    assert updated.json()['journal_code']=='ghrpj'
    verified=client.post('/journal/api/accounts/'+account['id']+'/verify',json={},headers=protected_headers(csrf))
    assert verified.status_code==200 and verified.json()['status']=='queued'
    assert client.get('/journal/api/accounts',headers={'Tailscale-User-Login':'another@example.test'}).status_code==403


def test_no_csrf_no_mutation_and_invalid_platform_url_is_rejected(web):
    client,store,initial,_=web
    password=change_initial_password(client,initial)
    csrf=login(client,password)
    data={'name':'Bad','platform':'bmc','username':'u','password':'p','submission_url':'https://untrusted.test/login'}
    assert client.post('/journal/api/accounts',json=data,headers=OWNER|ORIGIN).status_code==403
    assert client.post('/journal/api/accounts',json=data,headers=protected_headers(csrf)).status_code==422
    assert store.accounts()==[]


def test_scholarone_account_url_code_and_password_edit_are_safe(web):
    client,store,initial,_=web
    headers=protected_headers(login(client,change_initial_password(client,initial)))
    data={'name':'ScholarOne author','platform':'scholarone','username':'author@example.test',
        'password':'synthetic-scholarone-secret','base_url':'https://mc.manuscriptcentral.com/ageing/?PARAMS=session-value',
        'journal_code':'incorrect'}
    response=client.post('/journal/api/accounts',json=data,headers=headers)
    assert response.status_code==200
    account=response.json()
    assert account['base_url']=='https://mc.manuscriptcentral.com/ageing' and account['journal_code']=='ageing'
    assert account['password_configured'] and 'synthetic-scholarone-secret' not in response.text
    assert 'session-value' not in response.text
    edited=client.patch('/journal/api/accounts/'+account['id'],json={'name':'Renamed ScholarOne','password':''},headers=headers)
    assert edited.status_code==200 and store.account(account['id'],True)['password']=='synthetic-scholarone-secret'
    assert client.get('/journal/api/accounts/'+account['id'],headers=OWNER).json()['account']['journal_code']=='ageing'
    bad={**data,'base_url':'https://evil.mc.manuscriptcentral.com/ageing'}
    assert client.post('/journal/api/accounts',json=bad,headers=headers).status_code==422
    assert len(store.accounts())==1


def test_tailscale_stripped_paths_serve_page_and_mounted_assets(web):
    client,_,_,_=web
    # Serve forwards /journal/static/... as /static/...; mounted StaticFiles
    # must see the restored external prefix to match its /journal/static root.
    assert client.get('/static/app.js',headers=OWNER).status_code==200
    assert client.get('/static/app.css',headers=OWNER).status_code==200
    assert client.get('/api/session',headers=OWNER).status_code==200
    assert client.get('/journal/static/app.js',headers=OWNER).status_code==200


def test_only_pushplus_tests_and_failed_retries_are_allowed(web):
    client,store,initial,_=web
    csrf=login(client,change_initial_password(client,initial))
    headers=protected_headers(csrf)
    assert client.post('/journal/api/notifications/test',json={'channel':'pushplus'},headers=headers).status_code==422
    store.update_settings({'pushplus_token':'test-pushplus-token-secret'})
    assert client.post('/journal/api/notifications/test',json={'channel':'email'},headers=headers).status_code==422
    assert client.post('/journal/api/notifications/test',json={'channel':'wecom'},headers=headers).status_code==422
    assert client.post('/journal/api/notifications/test',json={'channel':'pushplus'},headers=headers).status_code==200
    with store.db() as db:
        row=dict(db.execute('SELECT * FROM notifications').fetchone())
        db.execute("UPDATE notifications SET status='accepted',receipt_id='receipt-123' WHERE id=?",(row['id'],))
    assert client.post('/journal/api/notifications/'+row['id']+'/retry',headers=headers).status_code==422

    with store.db() as db:
        db.execute("UPDATE notifications SET status='failed' WHERE id=?",(row['id'],))
    assert client.post('/journal/api/notifications/'+row['id']+'/retry',headers=headers).status_code==200
    with store.db() as db:
        db.execute("UPDATE notifications SET status='failed',channel='email' WHERE id=?",(row['id'],))
    assert client.post('/journal/api/notifications/'+row['id']+'/retry',headers=headers).status_code==422


def test_bmc_orcid_multiple_links_and_delete_require_csrf_and_keep_history(web):
    client,store,initial,_=web
    csrf=login(client,change_initial_password(client,initial))
    headers=protected_headers(csrf)
    urls=['https://submission.springernature.com/submission-details/11111111-1111-1111-1111-111111111111',
          'https://submission.springernature.com/submission-details/22222222-2222-2222-2222-222222222222']
    data={'name':'ORCID test','platform':'bmc','login_method':'orcid','username':'0000-0000-0000-0000',
          'password':'example-orcid-password','submission_urls':urls}
    response=client.post('/journal/api/accounts',json=data,headers=headers)
    assert response.status_code==200
    current=response.json()
    assert current['submission_urls']==urls and current['login_method']=='orcid'
    assert current['password_configured'] and 'example-orcid-password' not in response.text
    assert client.post('/journal/api/accounts',json={**data,'username':'bad'},headers=headers).status_code==422
    assert client.post('/journal/api/accounts',json={**data,'submission_urls':['https://evil.test/article']},headers=headers).status_code==422
    private=store.account(current['id'],True)
    store.apply_rows(private,[{'manuscript_number':'test','status':'Under Review','detail_url':urls[0]}],store.settings(),verified=True)
    assert client.delete('/journal/api/accounts/'+current['id'],headers=OWNER|ORIGIN).status_code==403
    assert client.delete('/journal/api/accounts/'+current['id'],headers=headers).status_code==200
    assert client.get('/journal/api/accounts',headers=OWNER).json()['items']==[]
    assert len(client.get('/journal/api/submissions?archived=true',headers=OWNER).json()['items'])==1


def _bmc_payload(name='BMC account', urls=None):
    data = {
        'name': name, 'platform': 'bmc', 'login_method': 'password',
        'username': 'author@example.test', 'password': 'synthetic-bmc-password',
    }
    if urls is not None:
        data['submission_urls'] = urls
    return data


def _bmc_url(number):
    return f'https://submission.springernature.com/submission-details/{number:08d}-1111-1111-1111-111111111111'


def _create_bmc(client, headers, name='BMC account', urls=None):
    response = client.post('/journal/api/accounts', json=_bmc_payload(name, urls), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_account_detail_returns_active_and_archived_submissions(web):
    client, store, initial, _ = web
    headers = protected_headers(login(client, change_initial_password(client, initial)))
    one, two = _bmc_url(101), _bmc_url(102)
    account = _create_bmc(client, headers, urls=[one, two])
    private = store.account(account['id'], True)
    store.apply_rows(private, [
        {'site': 'bmc', 'source': 'live', 'manuscript_number': 'BMC-101', 'status': 'Under Review',
         'detail_url': one, 'metadata': {'tracking_url': one}},
        {'site': 'bmc', 'source': 'live', 'manuscript_number': 'BMC-102', 'status': 'Under Review',
         'detail_url': two, 'metadata': {'tracking_url': two}},
    ], store.settings(), verified=True,
        target_results=[{'url': one, 'ok': True}, {'url': two, 'ok': True}])
    target = next(row for row in private['targets'] if row['url'] == two)

    removed = client.delete(f"/journal/api/accounts/{account['id']}/targets/{target['id']}", headers=headers)
    assert removed.status_code == 200, removed.text
    detail = client.get(f"/journal/api/accounts/{account['id']}", headers=OWNER)

    assert detail.status_code == 200
    assert set(detail.json()) == {'account', 'submissions'}
    assert {row['manuscript_number'] for row in detail.json()['submissions']} == {'BMC-101', 'BMC-102'}
    assert {row['manuscript_number'] for row in detail.json()['submissions'] if row['archived']} == {'BMC-102'}


def test_bmc_account_profile_patch_preserves_links_and_password_and_only_credentials_pause(web):
    client, store, initial, _ = web
    headers = protected_headers(login(client, change_initial_password(client, initial)))
    urls = [_bmc_url(111), _bmc_url(112)]
    account = _create_bmc(client, headers, urls=urls)
    private = store.account(account['id'], True)
    store.apply_rows(private, [], store.settings(), verified=True,
                     target_results=[{'url': url, 'ok': True} for url in urls])
    target_ids = {target['id'] for target in store.account(account['id'])['targets']}

    renamed = client.patch(f"/journal/api/accounts/{account['id']}", json={
        'name': 'Renamed BMC', 'journal_name': 'BMC Oncology',
    }, headers=headers)
    assert renamed.status_code == 200, renamed.text
    saved = store.account(account['id'], True)
    assert saved['name'] == 'Renamed BMC'
    assert saved['journal_name'] == 'BMC Oncology'
    assert saved['password'] == 'synthetic-bmc-password'
    assert saved['submission_urls'] == urls
    assert {target['id'] for target in saved['targets']} == target_ids
    assert saved['enabled'] == 1

    credentials = client.patch(f"/journal/api/accounts/{account['id']}", json={
        'username': 'updated@example.test', 'password': '',
    }, headers=headers)
    assert credentials.status_code == 200, credentials.text
    saved = store.account(account['id'], True)
    assert saved['username'] == 'updated@example.test'
    assert saved['password'] == 'synthetic-bmc-password'
    assert saved['enabled'] == 0
    assert {target['id'] for target in saved['targets']} == target_ids

    # Link state belongs to the target endpoints, so account PATCH rejects link fields.
    rejected = client.patch(f"/journal/api/accounts/{account['id']}", json={'submission_urls': []}, headers=headers)
    assert rejected.status_code == 422
    assert {target['id'] for target in store.account(account['id'])['targets']} == target_ids


def test_bmc_without_links_is_saveable_but_cannot_verify_or_refresh(web):
    client, store, initial, _ = web
    headers = protected_headers(login(client, change_initial_password(client, initial)))
    account = _create_bmc(client, headers, urls=[])

    verify = client.post(f"/journal/api/accounts/{account['id']}/verify", json={}, headers=headers)
    refresh = client.post('/journal/api/refresh', json={'account_id': account['id']}, headers=headers)

    assert verify.status_code == 422
    assert refresh.status_code == 422
    assert store.account(account['id'])['targets'] == []
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_target_batch_is_atomic_and_replace_creates_independent_history(web):
    client, store, initial, _ = web
    headers = protected_headers(login(client, change_initial_password(client, initial)))
    one, two, three, four = _bmc_url(121), _bmc_url(122), _bmc_url(123), _bmc_url(124)
    account = _create_bmc(client, headers, urls=[one, two])
    private = store.account(account['id'], True)
    store.apply_rows(private, [
        {'site': 'bmc', 'source': 'live', 'manuscript_number': 'BMC-122', 'status': 'Under Review',
         'detail_url': two, 'metadata': {'tracking_url': two}},
    ], store.settings(), verified=True, target_results=[{'url': one, 'ok': True}, {'url': two, 'ok': True}])
    target_two = next(row for row in private['targets'] if row['url'] == two)

    duplicate = client.post(f"/journal/api/accounts/{account['id']}/targets", json={'urls': [three, one]}, headers=headers)
    assert duplicate.status_code == 422
    assert {row['url'] for row in store.account(account['id'])['targets']} == {one, two}

    added = client.post(f"/journal/api/accounts/{account['id']}/targets", json={'urls': [four]}, headers=headers)
    assert added.status_code == 200, added.text
    assert {row['url'] for row in added.json()['targets']} == {one, two, four}

    replaced = client.patch(f"/journal/api/accounts/{account['id']}/targets/{target_two['id']}",
                            json={'url': three}, headers=headers)
    assert replaced.status_code == 200, replaced.text
    saved = replaced.json()
    assert saved['id'] == account['id']
    assert {row['url'] for row in saved['targets']} == {one, three, four}
    assert target_two['id'] not in {row['id'] for row in saved['targets']}
    history = client.get('/journal/api/submissions?archived=true', headers=OWNER).json()['items']
    archived = next(row for row in history if row['manuscript_number'] == 'BMC-122')
    assert archived['archived'] == 1

    private = store.account(account['id'], True)
    assert store.apply_rows(private, [
        {'site': 'bmc', 'source': 'live', 'manuscript_number': 'BMC-123', 'status': 'Under Review',
         'detail_url': three, 'metadata': {'tracking_url': three}},
    ], store.settings(), verified=True, target_results=[{'url': one, 'ok': True}, {'url': three, 'ok': True},
                                                          {'url': four, 'ok': True}]) == 0
    baseline = next(row for row in store.submissions() if row['manuscript_number'] == 'BMC-123')
    assert store.submission(baseline['id'])['events'][0]['kind'] == 'baseline'


def test_last_target_removal_archived_account_profile_and_target_limits(web):
    client, store, initial, _ = web
    headers = protected_headers(login(client, change_initial_password(client, initial)))
    one = _bmc_url(131)
    account = _create_bmc(client, headers, urls=[one])
    target = store.account(account['id'])['targets'][0]

    deleted = client.delete(f"/journal/api/accounts/{account['id']}/targets/{target['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()['account']['targets'] == []
    saved = store.account(account['id'])
    assert saved['targets'] == []
    assert saved['enabled'] == 0

    thirty_one = [_bmc_url(200 + i) for i in range(31)]
    over_limit = client.post(f"/journal/api/accounts/{account['id']}/targets", json={'urls': thirty_one}, headers=headers)
    assert over_limit.status_code == 422
    assert store.account(account['id'])['targets'] == []

    restored = client.post(f"/journal/api/accounts/{account['id']}/targets", json={'urls': [one]}, headers=headers)
    assert restored.status_code == 200, restored.text
    archived = client.post(f"/journal/api/accounts/{account['id']}/archive", json={}, headers=headers)
    assert archived.status_code == 200
    profile = client.patch(f"/journal/api/accounts/{account['id']}", json={'name': 'Archived profile edit'}, headers=headers)
    assert profile.status_code == 200, profile.text
    current_target = store.account(account['id'])['targets'][0]
    assert client.post(f"/journal/api/accounts/{account['id']}/targets", json={'urls': [_bmc_url(132)]}, headers=headers).status_code == 422
    assert client.patch(f"/journal/api/accounts/{account['id']}/targets/{current_target['id']}",
                        json={'url': _bmc_url(132)}, headers=headers).status_code == 422
    assert client.delete(f"/journal/api/accounts/{account['id']}/targets/{current_target['id']}", headers=headers).status_code == 422
    assert store.account(account['id'])['name'] == 'Archived profile edit'
