import sqlite3
from types import SimpleNamespace

import pytest

from deploy import install, upgrade
from journalcheck.server.store import Store
from tests.test_store import make_account


def test_upgrade_backs_up_database_key_and_existing_gateway_units_before_restart(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    store = Store(root)
    make_account(store)
    with store.db() as db:
        db.execute("DELETE FROM meta WHERE key='terminal_rules_v1'")
    monkeypatch.setattr('sys.argv', ['upgrade.py', '--runtime', str(root), '--wait-seconds', '0'])
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='Environment=JOURNALCHECK_GATEWAY_MODE=1\n')
    monkeypatch.setattr(upgrade.subprocess, 'run', run)
    upgrade.main()
    backup = next((root / 'upgrades').iterdir())
    with sqlite3.connect(backup / 'journalcheck.sqlite') as db:
        assert db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM meta WHERE key='terminal_rules_v1'").fetchone()
    assert (backup / 'fernet.key').read_bytes() == (root / 'keys/fernet.key').read_bytes()
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in backup.iterdir())
    assert calls[-2][2:] == ['restart', *upgrade.UNITS]
    assert all(call[0] == 'systemctl' for call in calls)


def test_upgrade_does_not_interrupt_running_job(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.queue_job()
    with store.db() as db:
        db.execute("UPDATE jobs SET status='running'")
    monkeypatch.setattr('sys.argv', ['upgrade.py', '--runtime', str(tmp_path), '--wait-seconds', '0'])
    monkeypatch.setattr(upgrade.subprocess, 'run', lambda *a, **k: pytest.fail('Must not restart'))
    with pytest.raises(SystemExit):
        upgrade.main()
    assert not (tmp_path / 'upgrades').exists()


def test_standalone_installer_cannot_overwrite_portal_auth(tmp_path, monkeypatch):
    unit = tmp_path / '.config/systemd/user/journalcheck-web.service'
    unit.parent.mkdir(parents=True)
    unit.write_text('Environment=JOURNALCHECK_GATEWAY_MODE=1\n')
    monkeypatch.setattr(install.Path, 'home', lambda:tmp_path)
    monkeypatch.setattr('sys.argv', ['install.py'])
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: pytest.fail('Must not deploy'))
    with pytest.raises(SystemExit):
        install.main()
