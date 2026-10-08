#!/usr/bin/env python3
"""Opt-in real ScholarOne check: fresh automatic login, then saved-session reuse.

Reads an existing account from SQLite in read-only mode; never updates production
data or sends notifications. Temporary browser state is removed on exit. Output
contains only counts, not credentials, journal responses or manuscript identities.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from cryptography.fernet import Fernet

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=REPO / '.runtime')
    parser.add_argument('--account-id', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.runtime.resolve()
    cipher = Fernet((root / 'keys/fernet.key').read_bytes())
    with sqlite3.connect((root / 'journalcheck.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM accounts WHERE id=? AND deleted=0 AND platform='scholarone'",
                         (args.account_id,)).fetchone()
        if not row:
            parser.error('请选择已有的 ScholarOne 账号。')
        account = dict(row)
        account.update(json.loads(cipher.decrypt(account.pop('config').encode())))
        encrypted = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
        network = json.loads(cipher.decrypt(encrypted.encode()))
    from journalcheck.server.browser_profiles import profile_path, tools_ready
    from journalcheck.server.scholarone import fetch_scholarone
    from journalcheck.server.adapters import AuthenticationError, ChallengeError, ParseError
    from journalcheck.server.store import Store
    import requests
    os.environ['JOURNALCHECK_BROWSER_TOOLS'] = str(root / 'browser-tools')
    if not tools_ready(root):
        parser.error('请先安装私有浏览器组件。')
    with tempfile.TemporaryDirectory(prefix='jc-check-') as directory:
        isolated = Path(directory)
        Store(isolated)  # Fresh encryption key, no production DB/credentials on disk.
        network['_runtime_root'] = str(isolated)
        profile = profile_path(isolated, account)
        assert not (profile / 'state.fernet').exists()
        try:
            first = fetch_scholarone(account, network)
            capsule_saved = (profile / 'state.fernet').is_file()
            second = fetch_scholarone(account, network)
        except (AuthenticationError, ParseError, requests.RequestException) as exc:
            category = ('challenge' if isinstance(exc, ChallengeError) else
                        'authentication' if isinstance(exc, AuthenticationError) else
                        'parse' if isinstance(exc, ParseError) else 'network')
            print(json.dumps({'ok':False, 'category':category}))
            return 1
        print(json.dumps({'ok':True, 'fresh_profile':True, 'first_count':len(first),
                          'session_saved':capsule_saved, 'second_count':len(second),
                          'same_manuscripts':{r['manuscript_number'] for r in first} ==
                                             {r['manuscript_number'] for r in second}}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
