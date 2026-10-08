import json
import threading
from journalcheck.server.store import Store, now, is_terminal
from journalcheck.server.worker import Worker, fetch_with_deadline
from tests.test_store import make_account, notification_config, submission


def test_worker_isolates_account_failure_and_retains_successful_results(tmp_path):
    store=Store(tmp_path)
    first=make_account(store)
    second=store.save_account({'name':'Second','platform':'em','username':'u','password':'p',
                               'base_url':'https://www.editorialmanager.com/demo/login.asp','journal_code':'demo'})
    store.apply_rows(first,[submission()],notification_config(),verified=True)
    store.apply_rows(second,[submission('EM-1')],notification_config(),verified=True)
    original=store.submissions()[0]['last_success_at']
    store.queue_job()
    def fetch(_,account,cfg):
        return {'ok':False,'category':'parse'} if account['id']==first['id'] else {'ok':True,'rows':[submission('EM-1',status='Revision Requested')]}
    worker=Worker(store,fetcher=fetch,sender=lambda *a:None)
    assert worker.run_job()
    with store.db() as db:
        job=dict(db.execute('SELECT * FROM jobs').fetchone())
    assert job['status']=='partial'
    assert job['progress']==job['total']==2
    assert store.account(first['id'])['error_category']=='parse'
    assert store.submission(next(r['id'] for r in store.submissions() if r['account_id']==first['id']))['submission']['status']=='Under Review'
    assert store.account(second['id'])['last_error'] is None


def test_worker_restart_marks_running_job_interrupted_and_requeues_delivery(tmp_path):
    store=Store(tmp_path)
    job=store.queue_job()
    cfg=notification_config()
    with store.db() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?",(job['id'],))
        store.enqueue_notification(db,'event',{'subject':'s','body':'b'},cfg)
        db.execute("UPDATE notifications SET status='sending'")
    Worker(store,sender=lambda *a:None).recover()
    with store.db() as db:
        assert db.execute('SELECT status FROM jobs').fetchone()[0]=='interrupted'
        assert db.execute('SELECT status FROM notifications').fetchone()[0]=='pending'
    assert store.queue_job()['id']!=job['id']


def test_pushplus_retries_failures_without_resending_accepted_notifications(tmp_path):
    store=Store(tmp_path)
    cfg=notification_config()
    store.update_settings(cfg)
    with store.db() as db:
        store.enqueue_notification(db,'first',{'subject':'first','body':'b'},cfg)
        store.enqueue_notification(db,'second',{'subject':'second','body':'b'},cfg)
    sent=[]
    def sender(channel,payload,settings):
        assert channel=='pushplus'
        sent.append(payload['subject'])
        if payload['subject']=='second':
            raise OSError('secret upstream error must never be exposed')
        return {'status':'accepted','receipt_id':'receipt-first'}
    worker=Worker(store,sender=sender)
    worker.deliver_one();worker.deliver_one()
    with store.db() as db:
        accepted=dict(db.execute("SELECT * FROM notifications WHERE event_id='first'").fetchone())
        pending=dict(db.execute("SELECT * FROM notifications WHERE event_id='second'").fetchone())
    assert accepted['status']=='accepted' and accepted['receipt_id']=='receipt-first'
    assert pending['status']=='pending' and pending['attempts']==1
    assert 'secret' not in pending['last_error']
    for _ in range(4):
        with store.db() as db:
            db.execute("UPDATE notifications SET next_attempt_at=? WHERE event_id='second'",(now(),))
        worker.deliver_one()
    with store.db() as db:
        item=dict(db.execute("SELECT * FROM notifications WHERE event_id='second'").fetchone())
    assert item['status']=='failed' and item['attempts']==5
    assert sent.count('first')==1


def test_article_identity_reaches_pushplus_mail_from_worker_change_event(tmp_path, monkeypatch):
    from journalcheck.server import delivery
    from tests.test_delivery import Session
    store = Store(tmp_path)
    store.update_settings(notification_config())
    account = store.save_account({'name':'EM login account', 'platform':'em', 'username':'u',
        'password':'p', 'base_url':'https://www.editorialmanager.com/ghrpj/default2.aspx'})
    article = submission('GHRPJ-26-0001', metadata={'journal_name':'Global Health Research Journal'})
    store.apply_rows(account, [article], store.settings(), verified=True)
    store.queue_job(account_id=account['id'])
    session = Session({'code':200,'data':'article-receipt'})
    monkeypatch.setattr(delivery.requests, 'Session', lambda:session)
    worker = Worker(store, fetcher=lambda *args:{'ok':True,'rows':[{**article,'status':'Revision Requested'}]})
    assert worker.run_job()
    assert worker.deliver_one()
    assert not worker.deliver_one()
    request = session.calls[0][1]['json']
    assert request['channel']=='mail' and request['template']=='txt'
    assert article['manuscript_number'] in request['title'] and article['title'] in request['title']
    assert f"文章：{article['title']}" in request['content']
    assert '期刊：Global Health Research Journal' in request['content']
    assert '账号：EM login account' in request['content']
    assert '状态：Under Review → Revision Requested' in request['content']
    with store.db() as db:
        assert db.execute('SELECT status,receipt_id FROM notifications').fetchone()[:] == ('accepted','article-receipt')


def test_pushplus_rejection_and_pause_do_not_retry_automatically(tmp_path):
    from journalcheck.server.delivery import DeliveryError
    store=Store(tmp_path)
    store.update_settings(notification_config())
    with store.db() as db:
        store.enqueue_notification(db,'event',{'subject':'s','body':'b'})
    def rejected(*args):
        raise DeliveryError('请检查 Token 和邮箱绑定。')
    worker=Worker(store,sender=rejected)
    assert worker.deliver_one()
    with store.db() as db:
        row=dict(db.execute('SELECT * FROM notifications').fetchone())
    assert row['status']=='failed' and row['attempts']==1 and row['next_attempt_at'] is None
    assert not worker.deliver_one()


def test_pause_during_verification_does_not_reenable_account(tmp_path):
    store=Store(tmp_path)
    account=make_account(store)
    with store.db() as db:
        db.execute('UPDATE accounts SET enabled=0,revision=revision+1 WHERE id=?',(account['id'],))
    store.apply_rows(account,[submission()],notification_config(),verified=True)
    assert not store.account(account['id'])['enabled']


def test_terminal_mapping_does_not_archive_reviewer_acceptance(tmp_path):
    assert not is_terminal('Reviewers Accepted')
    assert not is_terminal('Revision Completed')
    assert is_terminal('Accepted for publication')
    store=Store(tmp_path)
    account=make_account(store)
    cfg=notification_config()
    store.apply_rows(account,[submission(status='Accepted')],cfg,verified=True)
    assert len(store.submissions(True))==1
    store.apply_rows(account,[submission(status='Under Review')],cfg)
    assert len(store.submissions(False))==1


def test_deadline_kills_only_fetch_child_and_preserves_worker(tmp_path,monkeypatch):
    store=Store(tmp_path)
    finished=threading.Event()
    class FakeProcess:
        returncode=None
        def communicate(self,data):
            finished.wait(1)
            return json.dumps({'ok':False,'category':'timeout'}),''
        def kill(self):
            self.returncode=-9
            finished.set()
        def poll(self):
            return self.returncode
        def wait(self):
            return self.returncode
    monkeypatch.setattr('journalcheck.server.worker.subprocess.Popen',lambda *a,**k:FakeProcess())
    result=fetch_with_deadline(store,{}, {},timeout=0.02)
    assert result=={'ok':False,'category':'timeout'}
    assert finished.is_set()
    assert store.meta('worker_seen_at')


def test_missing_review_counts_keep_old_display_without_a_false_change(tmp_path):
    store=Store(tmp_path)
    account=make_account(store)
    cfg=notification_config()
    store.apply_rows(account,[submission(reviewer_invited=3)],cfg,verified=True)
    assert store.apply_rows(account,[submission(reviewer_invited=None)],cfg)==0
    row=store.submissions()[0]
    assert row['reviewer_invited'] is None
    assert row['metadata']['last_known_counts']['reviewer_invited']==3


def test_lower_bound_reviewer_count_change_is_not_lost(tmp_path):
    store=Store(tmp_path)
    account=make_account(store)
    cfg=notification_config()
    store.apply_rows(account,[submission(reviewer_invited=3,metadata={'reviewer_invited_display':'3+'})],cfg,verified=True)
    assert store.apply_rows(account,[submission(reviewer_invited=3)],cfg)==1
    event=store.submission(store.submissions()[0]['id'])['events'][-1]
    assert event['payload']['fields']==['reviewer_invited']


def test_configuration_change_during_fetch_discards_result_without_false_failure(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)
    cfg = notification_config()
    store.apply_rows(account, [submission()], cfg, verified=True)
    previous = store.submissions()[0]
    job = store.queue_job('refresh', account['id'])

    def fetch(_, current, settings):
        store.save_account({'name': 'Updated account name'}, current['id'])
        return {'ok': True, 'rows': [submission(status='Accepted')]}

    assert Worker(store, fetcher=fetch, sender=lambda *args: None).run_job()
    retained = store.submission(previous['id'])['submission']
    assert retained['status'] == 'Under Review'
    assert retained['last_success_at'] == previous['last_success_at']
    assert store.account(account['id'])['enabled']
    assert store.account(account['id'])['failures'] == 0
    with store.db() as db:
        results = json.loads(db.execute('SELECT results FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])
        assert results[0]['category'] == 'configuration'
        assert db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
