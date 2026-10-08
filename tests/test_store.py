import json
import shutil
import sqlite3
from pathlib import Path

from journalcheck.server.store import DEFAULTS, Store


def make_account(store):
    return store.save_account({
        "name": "Example journal",
        "platform": "aha",
        "username": "author@example.test",
        "password": "journal-password-secret",
        "base_url": "https://aha-journals.org/login",
    })


def notification_config():
    return {**DEFAULTS, 'pushplus_enabled':True, 'pushplus_token':'test-pushplus-token-secret'}


def submission(number="JHA-2026-0001", **changes):
    return {
        "site": "aha",
        "source": "Live Manuscripts",
        "manuscript_number": number,
        "title": "A cohort study of nutrition and health",
        "status": "Under Review",
        "status_date": "2026-09-01",
        "reviewer_invited": 2,
        "reviewer_accepted": 1,
        "review_reports_received": 0,
        "review_comments": ["The manuscript is under review."],
        "metadata": {},
        **changes,
    }


def test_first_baseline_records_without_queuing_notifications(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)

    changes = store.apply_rows(account, [submission()], notification_config(), verified=True)

    assert changes == 0
    with store.db() as db:
        events = db.execute("SELECT kind FROM events").fetchall()
        notifications = db.execute("SELECT * FROM notifications").fetchall()
    assert [row["kind"] for row in events] == ["baseline"]
    assert notifications == []
    assert store.account(account["id"])["baseline"] == 1
    assert store.account(account["id"])["enabled"] == 1


def test_observed_field_changes_create_event_and_queue_pushplus_only(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)
    cfg = notification_config()
    original = submission()
    store.apply_rows(account, [original], cfg, verified=True)
    changed = submission(
        status="Revision Requested",
        status_date="2026-09-28",
        reviewer_invited=3,
        reviewer_accepted=2,
        review_reports_received=1,
        review_comments=["Please clarify the exposure definition."],
    )

    assert store.apply_rows(account, [changed], cfg) == 1
    detail = store.submission(store.submissions()[0]["id"])
    assert len(detail["events"]) == 2
    event = detail["events"][-1]
    assert event["kind"] == "changed"
    assert set(event["payload"]["fields"]) == {
        "status", "status_date", "reviewer_invited", "reviewer_accepted",
        "review_reports_received", "review_comments",
    }
    with store.db() as db:
        notifications = db.execute("SELECT channel,status,event_id FROM notifications").fetchall()
        message = json.loads(db.execute('SELECT payload FROM notifications').fetchone()[0])
    assert len(notifications) == 1
    assert tuple(notifications[0]) == ("pushplus", "pending", event["id"])
    assert changed['title'] in message['subject']
    assert changed['manuscript_number'] in message['subject']
    assert f"文章：{changed['title']}" in message['body']
    assert f"期刊：{account['name']}" in message['body']
    assert f"账号：{account['name']}" in message['body']
    assert f"稿件编号：{changed['manuscript_number']}" in message['body']
    assert '状态：Under Review → Revision Requested' in message['body']
    assert 'Live Manuscripts' not in message['body']
    assert 'journal-password-secret' not in message['body']


def test_multiple_bmc_articles_have_distinct_mail_subjects_and_complete_titles(tmp_path):
    store = Store(tmp_path)
    account = store.save_account({'name':'My BMC account', 'platform':'bmc',
        'username':'author@example.test', 'password':'journal-password-secret',
        'submission_urls':[]})
    cfg = notification_config()
    title = 'Very long article title ' * 12
    articles = [submission('BMC-001', title=title, source='Example BMC Journal'),
                submission('BMC-002', title='A different article', source='Another BMC Journal')]
    store.apply_rows(account, articles, cfg, verified=True)
    assert store.apply_rows(account, [{**row,'status':'Revision Requested'} for row in articles], cfg) == 2
    with store.db() as db:
        messages = [json.loads(r[0]) for r in db.execute('SELECT payload FROM notifications ORDER BY rowid')]
    assert len(messages) == 2
    assert len(messages[0]['subject']) == 100 and messages[0]['subject'].endswith('…')
    assert 'BMC-001' in messages[0]['subject']
    assert 'BMC-002' in messages[1]['subject'] and 'A different article' in messages[1]['subject']
    assert ' '.join(title.split()) in messages[0]['body']
    assert '期刊：Example BMC Journal' in messages[0]['body']
    assert '期刊：Another BMC Journal' in messages[1]['body']
    assert '账号：My BMC account' in messages[0]['body']


def test_mail_uses_previous_title_and_only_reports_actual_field_changes(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)
    account['journal_name'] = 'Configured journal name'
    cfg = notification_config()
    original = submission()
    store.apply_rows(account, [original], cfg, verified=True)
    assert store.apply_rows(account, [submission(title='',reviewer_accepted=2)], cfg) == 1
    with store.db() as db:
        message = json.loads(db.execute('SELECT payload FROM notifications').fetchone()[0])
    assert original['title'] in message['subject'] and original['title'] in message['body']
    assert '期刊：Configured journal name' in message['body']
    assert '当前状态：Under Review' in message['body']
    assert '接受审稿人：1 → 2' in message['body']
    assert 'Under Review → Under Review' not in message['body']
    # A repeated observation must not queue a second message.
    assert store.apply_rows(account, [submission(title='',reviewer_accepted=2)], cfg) == 0
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 1


def test_failures_preserve_last_success_alert_at_threshold_and_recover(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)
    cfg = notification_config()
    store.apply_rows(account, [submission()], cfg, verified=True)
    last_success = store.account(account["id"])["last_success_at"]

    store.record_failure(account, "network", "temporary connection issue", cfg)
    after_first = store.account(account["id"])
    assert after_first["last_success_at"] == last_success
    assert after_first["failures"] == 1
    assert after_first["alerted"] == 0
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0

    store.record_failure(account, "network", "temporary connection issue", cfg)
    after_threshold = store.account(account["id"])
    assert after_threshold["last_success_at"] == last_success
    assert after_threshold["failures"] == 2
    assert after_threshold["alerted"] == 1
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1

    store.apply_rows(account, [submission()], cfg)
    recovered = store.account(account["id"])
    assert recovered["last_success_at"] != last_success
    assert recovered["failures"] == 0
    assert recovered["alerted"] == 0
    assert recovered["last_error"] is None
    with store.db() as db:
        notifications = db.execute("SELECT payload FROM notifications ORDER BY rowid").fetchall()
    assert len(notifications) == 2
    assert "恢复正常检查" in notifications[-1]["payload"]


def test_authentication_failure_alerts_immediately(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)
    store.record_failure(account, "authentication", "credentials rejected", notification_config())
    state = store.account(account["id"])
    assert state["failures"] == 1
    assert state["alerted"] == 1
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_missing_rows_are_retained_and_terminal_rows_are_archived(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)
    cfg = notification_config()
    live = submission("JHA-LIVE")
    later_missing = submission("JHA-MISSING")
    store.apply_rows(account, [live, later_missing], cfg, verified=True)

    store.apply_rows(account, [submission("JHA-LIVE", status="Rejected")], cfg)
    active = {row["manuscript_number"]: row for row in store.submissions()}
    archived = {row["manuscript_number"]: row for row in store.submissions(archived=True)}

    assert "JHA-LIVE" not in active
    assert archived["JHA-LIVE"]["archived"] == 1
    assert active["JHA-MISSING"]["missing"] == 1
    assert active["JHA-MISSING"]["archived"] == 0
    assert active["JHA-MISSING"]["status"] == "Under Review"
    detail = store.submission(active["JHA-MISSING"]["id"])
    assert any(event["kind"] == "missing" for event in detail["events"])


def test_missing_encryption_key_is_never_replaced(tmp_path):
    store = Store(tmp_path)
    key_path = tmp_path / "keys" / "fernet.key"
    key_path.unlink()

    try:
        Store(tmp_path)
    except RuntimeError as exc:
        assert "密钥缺失" in str(exc)
    else:
        raise AssertionError("Store unexpectedly replaced a lost encryption key")

    assert not key_path.exists()
    assert (tmp_path / "journalcheck.sqlite").exists()


def test_account_and_notification_secrets_are_encrypted_at_rest(tmp_path):
    store = Store(tmp_path)
    make_account(store)
    store.update_settings({'pushplus_token':'test-pushplus-token-secret'})
    cfg = notification_config()
    store.apply_rows(make_account(store), [submission()], cfg)

    database_bytes = b"".join(path.read_bytes() for path in tmp_path.glob("journalcheck.sqlite*"))
    for secret in (b"journal-password-secret", b"test-pushplus-token-secret"):
        assert secret not in database_bytes
    assert (tmp_path / "keys" / "fernet.key").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_only_one_queued_or_running_job_exists(tmp_path):
    store = Store(tmp_path)
    first = store.queue_job("refresh")
    second = store.queue_job("verify", "account-2")

    assert second["id"] == first["id"]
    assert second["reused"] is True
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] == 1


def test_backup_can_restore_database_with_original_encryption_key(tmp_path):
    store = Store(tmp_path / "source")
    original = make_account(store)
    backup = store.backup()
    store.save_account({
        "name": "Later account", "platform": "em", "username": "later@example.test",
        "password": "later-secret", "base_url": "https://editorialmanager.com/login",
        "journal_code": "demo",
    })

    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    shutil.copy2(backup, restore_root / "journalcheck.sqlite")
    (restore_root / "keys").mkdir()
    shutil.copy2(store.root / "keys" / "fernet.key", restore_root / "keys" / "fernet.key")
    restored = Store(restore_root)

    accounts = restored.accounts(private=True)
    assert len(accounts) == 1
    assert accounts[0]["id"] == original["id"]
    assert accounts[0]["password"] == "journal-password-secret"
    assert not any(item["name"] == "Later account" for item in accounts)
    assert backup.stat().st_mode & 0o777 == 0o600


def test_upgrade_removes_old_configs_cancels_old_queue_and_preserves_history(tmp_path):
    store=Store(tmp_path)
    account=make_account(store)
    store.apply_rows(account,[submission()],notification_config(),verified=True)
    with store.db() as db:
        db.execute('ALTER TABLE notifications DROP COLUMN receipt_id')
        db.execute("UPDATE meta SET value=? WHERE key='settings'",(store.pack({
            'interval_minutes':120,'smtp_password':'old-secret','wecom_webhook':'old-webhook'}),))
        for index,status in enumerate(('pending','sending','failed','sent')):
            db.execute('INSERT INTO notifications(id,channel,status,created_at,payload,event_id) VALUES (?,?,?,?,?,?)',
                       (str(index),'email',status,'2026-10-01','{}',str(index)))
    upgraded=Store(tmp_path)
    assert upgraded.settings()['interval_minutes']==120
    assert upgraded.settings()['pushplus_token']==''
    assert upgraded.accounts()[0]['id']==account['id']
    assert len(upgraded.submissions())==1
    with upgraded.db() as db:
        saved=upgraded.unpack(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        statuses=[row[0] for row in db.execute('SELECT status FROM notifications ORDER BY id')]
    assert 'smtp_password' not in saved and 'wecom_webhook' not in saved
    assert statuses==['cancelled','cancelled','cancelled','sent']
