import json

import pytest

from journalcheck.server.decisions import terminal_reason
from journalcheck.server.store import Store, is_terminal
from tests.test_store import make_account, notification_config, submission


@pytest.mark.parametrize('status', [
    'You need to choose another journal', 'Your submission has been rejected.',
    'Your manuscript has been rejected', 'Reject (06-Jul-2025)',
    'Immediate Reject and Transfer to Research Connections',
    'Reject with Transfer', 'Reject with Transfer Option', 'Transfer Recommended',
    'Recommended for transfer', 'Desk rejected', 'Reject without review',
    'Immediate Reject Outright', 'Rejected after review', '拒稿并建议转投',
    'Accepted for publication', 'Your submission has been accepted', 'Withdrawn',
])
def test_explicit_decisions_are_archived(status):
    assert is_terminal(status)


@pytest.mark.parametrize('status', [
    'Decision', 'With a Decision', 'Completed', 'Decision in Process',
    'Awaiting Decision', 'Reviewers Accepted', 'Reviewer declined invitation',
    'Revision Completed', 'Required Reviews Completed', 'Minor Revision',
    'Your submission is in peer review', 'Transfer Pending', 'Transferred in',
    'Manuscript received by transfer', 'Awaiting transfer decision',
    'Reject and Resubmit', 'Not rejected', 'Reject recommendation pending',
])
def test_ambiguous_queues_and_intermediate_steps_stay_active(status):
    assert not is_terminal(status)


def test_transfer_change_is_archived_with_history_and_one_identified_notification(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)
    cfg = notification_config()
    store.apply_rows(account, [submission()], cfg, verified=True)
    identity = store.submissions()[0]['id']
    updated = submission(status='You need to choose another journal', status_date=None)
    assert store.apply_rows(account, [updated], cfg) == 1
    assert not store.submissions()
    row = store.submissions(True)[0]
    assert row['id'] == identity and row['archive_reason'] == '拒稿／建议转投'
    assert row['manual_archived'] == 0
    assert len(store.submission(identity)['events']) == 2
    assert store.apply_rows(account, [updated], cfg) == 0
    with store.db() as db:
        notifications = db.execute('SELECT payload FROM notifications').fetchall()
    assert len(notifications) == 1
    assert updated['title'] in json.loads(notifications[0][0])['body']


def test_rules_upgrade_is_quiet_idempotent_and_preserves_manual_history(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)
    cfg = notification_config()
    store.apply_rows(account, [submission(status='You need to choose another journal'),
                               submission('manual'), submission('live')], cfg, verified=True)
    with store.db() as db:
        db.execute('UPDATE submissions SET archived=0')  # Simulate the old terminal rules.
        db.execute("UPDATE submissions SET archived=1,manual_archived=1 WHERE manuscript_number='manual'")
        db.execute("DELETE FROM meta WHERE key='terminal_rules_v1'")
        before = [tuple(row) for row in db.execute('SELECT id,payload,first_seen_at,last_success_at FROM submissions ORDER BY id')]
        history = [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY id')]
    for _ in range(2):
        upgraded = Store(tmp_path)
        assert len(upgraded.submissions(True)) == 2
        assert len(upgraded.submissions()) == 1
        with upgraded.db() as db:
            assert before == [tuple(row) for row in db.execute('SELECT id,payload,first_seen_at,last_success_at FROM submissions ORDER BY id')]
            assert history == [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY id')]
            assert db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM audit WHERE action='submission_auto_archived_rules_v1'").fetchone()[0] == 1


def test_missing_status_date_does_not_cause_false_change_on_return(tmp_path):
    store = Store(tmp_path)
    account = make_account(store)
    cfg = notification_config()
    store.apply_rows(account, [submission()], cfg, verified=True)
    before = store.submissions()[0]
    assert store.apply_rows(account, [submission(status_date=None)], cfg) == 0
    assert store.submissions()[0]['status_date'] == before['status_date']
    assert store.apply_rows(account, [submission()], cfg) == 0
    assert store.apply_rows(account, [submission(status='Revision Requested', status_date=None)], cfg) == 1
    assert store.submissions()[0]['status_since_source'] == 'observed'
