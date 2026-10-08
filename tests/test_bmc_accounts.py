import json
from types import SimpleNamespace

import pytest

from journalcheck.server import adapters
from journalcheck.server.store import Store,DEFAULTS
from journalcheck.server.worker import Worker
from tests.test_store import notification_config,submission
from tests.test_adapters import FakeSession,html

ONE='https://submission.springernature.com/submission-details/11111111-1111-1111-1111-111111111111'
TWO='https://submission.springernature.com/submission-details/22222222-2222-2222-2222-222222222222'


def account(store,urls=(ONE,TWO)):
    return store.save_account({'name':'BMC','platform':'bmc','username':'example@example.test','password':'test-only',
                              'login_method':'password','submission_urls':list(urls)})


def tracked(number,url,**changes):
    return submission(number,detail_url=url,metadata={'tracking_url':url},**changes)


def test_bmc_checks_multiple_links_with_one_shared_login_session(monkeypatch):
    class Session(FakeSession):
        def __enter__(self):return self
        def __exit__(self,*args):pass
    session=Session({
        ('GET',ONE):(html('bmc_login_email.html'),ONE),
        ('POST','https://idp-personal-authenticator.springernature.com/email'):(html('bmc_login_password.html'),'https://idp.test/password'),
        ('POST','https://idp-personal-authenticator.springernature.com/password'):(html('bmc_detail.html'),ONE),
        ('GET',TWO):(html('bmc_detail.html'),TWO),
    })
    monkeypatch.setattr(adapters,'_AdapterSession',lambda cfg:session)
    result=adapters.fetch_account_result({'name':'BMC','platform':'bmc','username':'u','password':'p','submission_urls':[ONE,TWO]}, {})
    assert not result['partial'] and len(result['rows'])==2
    assert [r['metadata']['tracking_url'] for r in result['rows']]==[ONE,TWO]
    assert len([c for c in session.calls if c[0]=='POST'])==2


def test_partial_link_failure_preserves_its_state_and_new_link_has_silent_baseline(tmp_path):
    store=Store(tmp_path)
    current=account(store)
    cfg=notification_config()
    success=[{'url':ONE,'ok':True},{'url':TWO,'ok':True}]
    store.apply_rows(current,[tracked('ONE',ONE),tracked('TWO',TWO)],cfg,verified=True,target_results=success)
    saved=store.submission(next(r['id'] for r in store.submissions() if r['manuscript_number']=='TWO'))['submission']
    partial=[{'url':ONE,'ok':True},{'url':TWO,'ok':False,'category':'access','message':'无访问权限。'}]
    assert store.apply_rows(current,[tracked('ONE',ONE,status='Revision Requested')],cfg,target_results=partial)==1
    retained=store.submission(saved['id'])['submission']
    assert retained['last_success_at']==saved['last_success_at'] and not retained['missing']
    assert retained['status']=='Under Review'
    assert store.account(current['id'])['targets'][1]['last_error']=='无访问权限。'
    three=ONE.replace('11111111-1111-1111-1111-111111111111','33333333-3333-3333-3333-333333333333')
    store.save_account({**current,'submission_urls':[ONE,TWO,three]},current['id'])
    current=store.account(current['id'],True)
    assert current['enabled']
    assert store.apply_rows(current,[tracked('ONE',ONE,status='Revision Requested'),tracked('TWO',TWO),tracked('THREE',three)],cfg,
                            verified=True,target_results=[*success,{'url':three,'ok':True}])==0
    detail=store.submission(next(r['id'] for r in store.submissions() if r['manuscript_number']=='THREE'))
    assert detail['events'][0]['kind']=='baseline'


def test_delete_scrubs_credentials_preserves_history_and_blocks_late_result(tmp_path):
    store=Store(tmp_path)
    current=account(store,[ONE])
    store.apply_rows(current,[tracked('ONE',ONE)],DEFAULTS,verified=True,target_results=[{'url':ONE,'ok':True}])
    original=store.submissions()[0]
    store.delete_account(current['id'])
    assert store.account(current['id']) is None and store.accounts()==[]
    assert store.submissions()==[] and store.submissions(True)[0]['id']==original['id']
    assert store.submission(original['id'])['events']
    assert store.apply_rows(current,[tracked('ONE',ONE,status='Accepted')],DEFAULTS)==0
    with store.db() as db:
        config=db.execute('SELECT config FROM accounts').fetchone()[0]
    assert store.unpack(config)=={}


def test_worker_keeps_successful_links_when_one_link_fails(tmp_path):
    store=Store(tmp_path)
    current=account(store)
    store.queue_job('verify',current['id'])
    def fetch(*args):
        return {'ok':True,'rows':[tracked('ONE',ONE)],'partial':True,
                'targets':[{'url':ONE,'ok':True},{'url':TWO,'ok':False,'category':'access','message':'无访问权限。'}]}
    worker=Worker(store,fetcher=fetch,sender=lambda *args:None)
    assert worker.run_job()
    assert len(store.submissions())==1 and not store.account(current['id'])['enabled']
    with store.db() as db:
        job=dict(db.execute('SELECT * FROM jobs').fetchone())
    assert job['status']=='partial' and json.loads(job['results'])[0]['targets'][1]['category']=='access'


def test_removed_link_archives_history_without_deleting_it(tmp_path):
    store=Store(tmp_path)
    current=account(store)
    store.apply_rows(current,[tracked('ONE',ONE),tracked('TWO',TWO)],DEFAULTS,target_results=[{'url':ONE,'ok':True},{'url':TWO,'ok':True}])
    store.save_account({**current,'submission_urls':[ONE]},current['id'])
    assert len(store.submissions())==1 and len(store.submissions(True))==1
    assert len(store.account(current['id'])['targets'])==1
    store.apply_rows(current,[tracked('ONE',ONE),tracked('TWO',TWO,status='Revision Requested')],DEFAULTS,
                     target_results=[{'url':ONE,'ok':True},{'url':TWO,'ok':True}])
    assert len(store.submissions())==1 and len(store.submissions(True))==1


def test_target_store_operations_keep_config_and_target_rows_in_sync_atomically(tmp_path):
    store=Store(tmp_path)
    current=account(store,[])
    three=ONE.replace('11111111-1111-1111-1111-111111111111','33333333-3333-3333-3333-333333333333')
    four=ONE.replace('11111111-1111-1111-1111-111111111111','44444444-4444-4444-4444-444444444444')

    store.add_targets(current['id'],[three,four])
    saved=store.account(current['id'])
    assert saved['submission_urls']==[three,four]
    assert [target['url'] for target in saved['targets']]==[three,four]
    with store.db() as db:
        packed=db.execute('SELECT config FROM accounts WHERE id=?',(current['id'],)).fetchone()[0]
        config=store.unpack(packed)
        assert config['submission_urls']==[three,four]

    with pytest.raises(ValueError):
        store.add_targets(current['id'],[ONE,three])
    unchanged=store.account(current['id'])
    assert unchanged['submission_urls']==[three,four]
    assert [target['url'] for target in unchanged['targets']]==[three,four]


def test_replaced_target_archives_old_submission_and_rejects_stale_result(tmp_path):
    store=Store(tmp_path)
    current=account(store,[ONE])
    store.apply_rows(current,[tracked('ONE',ONE)],DEFAULTS,verified=True,target_results=[{'url':ONE,'ok':True}])
    original=store.submissions()[0]
    stale_account=store.account(current['id'],True)
    old_target=stale_account['targets'][0]
    two=ONE.replace('11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222')

    store.replace_target(current['id'],old_target['id'],two)

    saved=store.account(current['id'])
    assert [target['url'] for target in saved['targets']]==[two]
    assert saved['revision']>stale_account['revision']
    detail=store.submission(original['id'])
    assert detail['submission']['archived']==1
    assert store.apply_rows(stale_account,[tracked('ONE',ONE,status='Accepted')],DEFAULTS,
                            target_results=[{'url':ONE,'ok':True}])==0
    store.record_failure(stale_account,'network','synthetic late failure',DEFAULTS)
    assert store.submission(original['id'])['submission']['status']=='Under Review'
    assert store.account(current['id'])['failures']==0


def test_removing_last_target_pauses_account_and_keeps_target_history(tmp_path):
    store=Store(tmp_path)
    current=account(store,[ONE])
    target=current['targets'][0]

    store.remove_target(current['id'],target['id'])

    saved=store.account(current['id'])
    assert saved['targets']==[]
    assert saved['submission_urls']==[]
    assert saved['enabled']==0
    assert saved['revision']>current['revision']
    with store.db() as db:
        historical=db.execute('SELECT enabled FROM account_targets WHERE id=?',(target['id'],)).fetchone()
        assert historical['enabled']==0


def test_orcid_credentials_never_submitted_to_an_unconfirmed_host():
    from journalcheck.server.orcid import login_orcid
    class Locator:
        def count(self):return 1
        def get_attribute(self,key):return 'https://evil.example/orcid/pre-auth'
    page=SimpleNamespace(locator=lambda *args:Locator())
    with pytest.raises(adapters.AuthenticationError):
        login_orcid(page,'test-orcid','test-password')


def test_orcid_delegation_is_explicit(monkeypatch):
    import journalcheck.server.orcid as orcid
    called=[]
    monkeypatch.setattr(orcid,'fetch_orcid_result',lambda a,n:called.append(a['login_method']) or {'ok':True,'rows':[]})
    assert adapters.fetch_account_result({'platform':'bmc','login_method':'orcid'}, {})=={'ok':True,'rows':[]}
    assert called==['orcid']


def test_readded_link_first_observation_is_silent_even_with_existing_history(tmp_path):
    store=Store(tmp_path)
    current=account(store,[ONE])
    cfg=notification_config()
    store.apply_rows(current,[tracked('ONE',ONE)],cfg,verified=True,target_results=[{'url':ONE,'ok':True}])
    store.remove_target(current['id'],current['targets'][0]['id'])
    store.add_targets(current['id'],[ONE])
    current=store.account(current['id'],True)
    assert store.apply_rows(current,[tracked('ONE',ONE,status='Revision Requested')],cfg,
                            target_results=[{'url':ONE,'ok':True}])==0
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]==0
    assert store.submission(store.submissions(True)[0]['id'])['events'][-1]['kind']=='baseline'
