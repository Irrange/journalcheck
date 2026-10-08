#!/usr/bin/env python3
"""Back up and restart an existing deployment, preserving gateway and Serve config."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

REPO = Path(__file__).resolve().parents[1]
UNITS = ('journalcheck-web.service', 'journalcheck-worker.service')


def systemctl(*args):
    subprocess.run(['systemctl', '--user', *args], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=REPO / '.runtime')
    parser.add_argument('--wait-seconds', type=int, default=660)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.runtime.resolve()
    database = root / 'journalcheck.sqlite'
    if not database.is_file():
        parser.error('没有既有数据库；首次部署请使用 install.py。')
    deadline = time.monotonic() + args.wait_seconds
    print('等待正在执行的检查结束…', flush=True)
    while True:
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
            running = db.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone()
        if not running:
            break
        if time.monotonic() >= deadline:
            parser.error('检查仍在执行，本次未重启服务。')
        time.sleep(2)
    snapshot = root / 'upgrades' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S-archive-autologin')
    snapshot.mkdir(parents=True, mode=0o700)
    # Do not construct Store before the backup: startup applies data migrations.
    with sqlite3.connect(database) as source, sqlite3.connect(snapshot / 'journalcheck.sqlite') as destination:
        source.backup(destination)
        if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('备份完整性检查失败，未重启服务。')
    shutil.copy2(root / 'keys/fernet.key', snapshot / 'fernet.key')
    for unit in UNITS:
        result = subprocess.run(['systemctl', '--user', 'cat', unit], check=True, capture_output=True, text=True)
        (snapshot / unit).write_text(result.stdout)
    (snapshot / 'manifest.json').write_text(json.dumps({'created_at':datetime.now(timezone.utc).isoformat(),
        'purpose':'Backup before data migration; existing systemd and Tailscale routes unchanged.'}))
    for path in snapshot.iterdir():
        path.chmod(0o600)
    systemctl('restart', *UNITS)
    systemctl('is-active', *UNITS)
    print('升级完成，数据库和密钥备份已保存；现有认证配置及访问路由保持不变。')


if __name__ == '__main__':
    main()
