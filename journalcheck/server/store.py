from __future__ import annotations
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .security import Secrets, hash_password, private_write
from .decisions import terminal_reason


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def later(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='microseconds')


def uid() -> str:
    return uuid.uuid4().hex


def field_changed(field, before, after):
    if before is None:
        return True
    if field == 'status_date' and not after.get(field):
        return False
    if field in ('reviewer_invited','reviewer_accepted','review_reports_received'):
        if after.get(field) is None:
            return False
        previous = before.get(field)
        if previous is None:
            previous = before.get('metadata',{}).get('last_known_counts',{}).get(field)
        old_display = before.get('metadata',{}).get(field+'_display',str(previous))
        new_display = after.get('metadata',{}).get(field+'_display',str(after[field]))
        return previous != after[field] or old_display != new_display
    return before.get(field) != after.get(field)


def change_line(field, before, after):
    labels = {'status':'状态','status_date':'状态日期','reviewer_invited':'邀请审稿人',
              'reviewer_accepted':'接受审稿人','review_reports_received':'收到报告','review_comments':'审稿意见'}
    if field == 'review_comments':
        return '审稿意见内容已更新。'
    def value(record):
        if not record or record.get(field) is None:
            return '未提供'
        return str(record.get('metadata',{}).get(field+'_display', record[field]))
    return f"{labels.get(field,field)}：{value(before)} → {value(after)}"


def submission_notification(account, before, after, fields):
    """Identify the article in both the inbox subject and the plain-text body."""
    before = before or {}

    def text(value):
        return ' '.join(str(value or '').split())

    title = text(after.get('title')) or text(before.get('title')) or '标题暂未提供'
    number = text(after.get('manuscript_number')) or text(before.get('manuscript_number'))
    metadata = after.get('metadata') or {}
    old_metadata = before.get('metadata') or {}
    journal = (text(account.get('journal_name')) or text(metadata.get('journal_name'))
               or text(old_metadata.get('journal_name')))
    if not journal and account['platform'] == 'bmc':
        journal = text(after.get('source')) or text(before.get('source'))
    if not journal and account['platform'] in ('em', 'scholarone'):
        journal = text(account.get('journal_code')).upper()
    if account['platform'] == 'scholarone':
        from .journal_names import scholarone_name
        journal = scholarone_name(account.get('journal_code', ''), journal)
    journal = journal or text(account['name'])
    action = '稿件更新' if before else '发现新稿件'
    subject = f"journalcheck {action} · {number} · {title}"
    # Stay within the standard PushPlus title limit; the body keeps the full title.
    if len(subject) > 100:
        subject = subject[:99] + '…'
    body = [f'文章：{title}', f'期刊：{journal}', f"账号：{text(account['name'])}",
            f'稿件编号：{number}', f"当前状态：{text(after.get('status'))}", '', '本次变化：']
    body.extend(change_line(field, before, after) for field in fields)
    return {'subject':subject, 'body':'\n'.join(body)}


def is_terminal(status: str) -> bool:
    return terminal_reason(status) is not None


def normalized_date(value: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.date().isoformat() if 'T' not in value and ' ' not in value else parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).isoformat()
    except (ValueError,TypeError):
        pass
    for pattern in ('%d-%b-%Y','%d %b %Y','%d %B %Y','%b %d, %Y','%d/%m/%Y'):
        try:
            return datetime.strptime(value,pattern).date().isoformat()
        except (ValueError,TypeError):
            continue
    return None


DEFAULTS = dict(interval_minutes=60, auto_refresh=True, http_proxy='', https_proxy='', no_proxy='',
                pushplus_token='', pushplus_option='', pushplus_enabled=True)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,name TEXT NOT NULL,platform TEXT NOT NULL,
 config TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,archived INTEGER NOT NULL DEFAULT 0,
 baseline INTEGER NOT NULL DEFAULT 0,last_attempt_at TEXT,last_success_at TEXT,last_error TEXT,
 error_category TEXT,failures INTEGER NOT NULL DEFAULT 0,alerted INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS submissions(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,manuscript_number TEXT NOT NULL,
 payload TEXT NOT NULL,first_seen_at TEXT NOT NULL,last_success_at TEXT NOT NULL,status_since TEXT NOT NULL,
 status_since_source TEXT NOT NULL,missing INTEGER NOT NULL DEFAULT 0,archived INTEGER NOT NULL DEFAULT 0,manual_archived INTEGER NOT NULL DEFAULT 0,
 UNIQUE(account_id,manuscript_number));
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,submission_id TEXT NOT NULL,created_at TEXT NOT NULL,
 kind TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,account_id TEXT,status TEXT NOT NULL,
 created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,progress INTEGER NOT NULL DEFAULT 0,
 total INTEGER NOT NULL DEFAULT 0,results TEXT NOT NULL DEFAULT '[]',error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs((1)) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,channel TEXT NOT NULL,status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at TEXT,created_at TEXT NOT NULL,last_error TEXT,
 payload TEXT NOT NULL,event_id TEXT NOT NULL,UNIQUE(event_id,channel));
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vault_unlocks(sid TEXT PRIMARY KEY,expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,action TEXT NOT NULL,target TEXT);
CREATE TABLE IF NOT EXISTS account_targets(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,url TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1,baseline INTEGER NOT NULL DEFAULT 0,last_attempt_at TEXT,last_success_at TEXT,
 last_error TEXT,error_category TEXT,UNIQUE(account_id,url));
CREATE INDEX IF NOT EXISTS event_history ON events(submission_id,created_at);
'''


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
        self.secrets = Secrets(self.root)
        self.path = self.root / 'journalcheck.sqlite'
        with self.db() as db:
            db.executescript(SCHEMA)
            for table,name,definition in [('accounts','revision','INTEGER NOT NULL DEFAULT 0'),
                                          ('accounts','deleted','INTEGER NOT NULL DEFAULT 0'),
                                          ('submissions','manual_archived','INTEGER NOT NULL DEFAULT 0'),
                                          ('notifications','receipt_id','TEXT')]:
                columns = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
                if name not in columns:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('settings', self.pack(DEFAULTS)))
            # Retire the old server channels without replaying historic messages.
            saved = self.unpack(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
            if any(key not in DEFAULTS for key in saved):
                saved = {**DEFAULTS, **{key:value for key,value in saved.items() if key in DEFAULTS}}
                db.execute("UPDATE meta SET value=? WHERE key='settings'", (self.pack(saved),))
                self.audit(db, 'pushplus_migration')
            db.execute("UPDATE notifications SET status='cancelled',next_attempt_at=NULL,last_error='旧通知渠道已停用；历史记录保留。' "
                       "WHERE channel IN ('email','wecom') AND status IN ('pending','sending','failed')")
            for account in db.execute("SELECT id,config FROM accounts WHERE platform='bmc' AND deleted=0").fetchall():
                config = self.unpack(account['config'])
                urls = config.get('submission_urls') or ([config['submission_url']] if config.get('submission_url') else [])
                for url in urls:
                    db.execute('INSERT OR IGNORE INTO account_targets(id,account_id,url) VALUES (?,?,?)', (uid(),account['id'],url))
                    # Existing successfully observed submissions already have their baseline.
                    for row in db.execute('SELECT payload,last_success_at FROM submissions WHERE account_id=?',(account['id'],)).fetchall():
                        data = json.loads(row['payload'])
                        if data.get('metadata',{}).get('tracking_url',data.get('detail_url')) == url:
                            db.execute('UPDATE account_targets SET baseline=1,last_success_at=COALESCE(last_success_at,?),last_attempt_at=COALESCE(last_attempt_at,?) WHERE account_id=? AND url=?',
                                       (row['last_success_at'],row['last_success_at'],account['id'],url))
            # One-time, idempotent reconciliation of previously missed decisions.
            # Preserve IDs, payloads, timelines, manual choices and notification history.
            if not db.execute("SELECT 1 FROM meta WHERE key='terminal_rules_v1'").fetchone():
                for row in db.execute('SELECT id,payload FROM submissions WHERE archived=0 AND manual_archived=0').fetchall():
                    if is_terminal(json.loads(row['payload']).get('status', '')):
                        db.execute('UPDATE submissions SET archived=1 WHERE id=?', (row['id'],))
                        self.audit(db, 'submission_auto_archived_rules_v1', row['id'])
                db.execute("INSERT INTO meta VALUES ('terminal_rules_v1', ?)", (now(),))
            db.execute('PRAGMA user_version=4')
        self.secure_files()

    def secure_files(self):
        for path in self.root.glob('journalcheck.sqlite*'):
            path.chmod(0o600)

    @contextmanager
    def db(self, immediate=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        try:
            if immediate:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def pack(self, obj):
        return self.secrets.encrypt(json.dumps(obj, ensure_ascii=False))

    def unpack(self, value):
        return json.loads(self.secrets.decrypt(value))

    def meta(self, key, default=None):
        with self.db() as db:
            r = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return r['value'] if r else default

    def set_meta(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))

    def settings(self, db=None):
        if db is None:
            saved = self.unpack(self.meta('settings'))
        else:
            saved = self.unpack(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        return {key:saved.get(key,value) for key,value in DEFAULTS.items()}

    def public_settings(self):
        cfg = self.settings()
        cfg['pushplus_token_configured'] = bool(cfg.pop('pushplus_token'))
        # Proxy URLs can themselves contain passwords; only a configured flag is public in that case.
        from urllib.parse import urlsplit
        for key in ('http_proxy', 'https_proxy'):
            if cfg[key] and urlsplit(cfg[key]).username:
                cfg[key] = ''
                cfg[key + '_configured'] = True
        return cfg

    def update_settings(self, values):
        cfg = self.settings()
        for key in DEFAULTS:
            if key in values:
                if key == 'pushplus_token' and not values[key]:
                    continue
                cfg[key] = values[key]
        with self.db() as db:
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (self.pack(cfg),))
            db.execute("DELETE FROM meta WHERE key='next_due_at'")
            self.audit(db, 'settings_updated')
        return self.public_settings()

    @staticmethod
    def audit(db, action, target=None):
        db.execute('INSERT INTO audit VALUES (?,?,?,?)', (uid(), now(), action, target))

    def _account_item(self, row, db, private=False):
        item = dict(row)
        config = self.unpack(item.pop('config'))
        item.update(config)
        item['journal_name'] = config.get('journal_name', '')
        if item['platform'] == 'scholarone':
            from .journal_names import scholarone_name
            item['journal_name'] = scholarone_name(config.get('journal_code', ''), item['journal_name'])
        item['login_method'] = config.get('login_method', 'password')
        item['submission_urls'] = config.get('submission_urls',
            [config['submission_url']] if config.get('submission_url') else [])
        item['targets'] = [dict(r) for r in db.execute(
            'SELECT * FROM account_targets WHERE account_id=? AND enabled=1 ORDER BY rowid', (item['id'],))]
        if not private:
            item['password_configured'] = bool(item.pop('password', ''))
        return item

    def accounts(self, private=False):
        with self.db() as db:
            return [self._account_item(row, db, private) for row in
                    db.execute('SELECT * FROM accounts WHERE deleted=0 ORDER BY rowid').fetchall()]

    def account(self, account_id, private=False):
        with self.db() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=? AND deleted=0', (account_id,)).fetchone()
            return self._account_item(row, db, private) if row else None

    def account_detail(self, account_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=? AND deleted=0', (account_id,)).fetchone()
            if not row:
                raise KeyError(account_id)
            account = self._account_item(row, db)
            rows = db.execute('SELECT s.*,a.name account_name,a.platform FROM submissions s '
                              'JOIN accounts a ON a.id=s.account_id WHERE s.account_id=? '
                              'ORDER BY s.archived,s.last_success_at DESC', (account_id,)).fetchall()
            submissions = [self._submission_item(r) for r in rows]
        return {'account': account, 'submissions': submissions}

    @staticmethod
    def _submission_item(row):
        item = {**json.loads(row['payload']), **{k:row[k] for k in row.keys() if k != 'payload'}}
        item['archive_reason'] = ('手动归档／停止追踪' if item['manual_archived'] else
                                  terminal_reason(item.get('status', ''))) if item['archived'] else None
        return item

    @staticmethod
    def _archive_target_submissions(db, account_id, url):
        for row in db.execute('SELECT id,payload FROM submissions WHERE account_id=?', (account_id,)).fetchall():
            data = json.loads(row['payload'])
            if data.get('metadata', {}).get('tracking_url', data.get('detail_url')) == url:
                db.execute('UPDATE submissions SET archived=1,manual_archived=1 WHERE id=?', (row['id'],))

    def save_account(self, values, account_id=None):
        # Merge inside the write transaction; a credentials edit cannot discard a concurrently added link.
        identity = account_id or uid()
        with self.db(True) as db:
            row = db.execute('SELECT * FROM accounts WHERE id=? AND deleted=0', (identity,)).fetchone()
            old = self._account_item(row, db, True) if row else None
            if account_id and not old:
                raise KeyError(account_id)
            platform = values.get('platform', (old or {}).get('platform'))
            if old and platform != old['platform']:
                raise ValueError('已有账号的平台不能修改；请新增账号。')
            config = {k: values.get(k, (old or {}).get(k, '')) for k in
                      ('username', 'password', 'base_url', 'journal_code', 'journal_name')}
            config['login_method'] = values.get('login_method', (old or {}).get('login_method', 'password'))
            if old and not config['password']:
                config['password'] = old['password']
            if not config['password']:
                raise ValueError('请填写期刊账号密码。')
            if platform == 'em':
                from .account_urls import editorial_manager_url
                config['base_url'], config['journal_code'] = editorial_manager_url(config['base_url'])
            elif platform == 'scholarone':
                from .account_urls import scholarone_url
                config['base_url'], config['journal_code'] = scholarone_url(config['base_url'])
            if 'submission_urls' in values:
                urls = values['submission_urls']
            elif 'submission_url' in values:
                urls = [values['submission_url']] if values['submission_url'] else []
            else:
                urls = (old or {}).get('submission_urls', [])
            if platform == 'bmc':
                from .account_urls import bmc_submission_url
                urls = list(dict.fromkeys(bmc_submission_url(url) for url in urls))
                if len(urls) > 30:
                    raise ValueError('每个账号最多追踪 30 篇文章。')
                if old and old['archived'] and urls != old['submission_urls']:
                    raise ValueError('账号已归档，请先恢复账号再管理文章。')
            config['submission_urls'] = urls
            config['submission_url'] = next(iter(urls), '')
            changed_login = bool(old and any(config[k] != old.get(k, 'password' if k == 'login_method' else '')
                                             for k in ('username','password','base_url','journal_code','login_method')))
            name = values.get('name', (old or {}).get('name'))
            if old:
                db.execute('UPDATE accounts SET name=?,config=?,revision=revision+1,enabled=CASE WHEN ? THEN 0 ELSE enabled END WHERE id=?',
                           (name, self.pack(config), changed_login or (platform == 'bmc' and not urls), identity))
            else:
                db.execute('INSERT INTO accounts(id,name,platform,config) VALUES (?,?,?,?)',
                           (identity, name, platform, self.pack(config)))
            if platform == 'bmc':
                previous_urls = {r[0] for r in db.execute(
                    'SELECT url FROM account_targets WHERE account_id=? AND enabled=1', (identity,))}
                db.execute('UPDATE account_targets SET enabled=0 WHERE account_id=?', (identity,))
                for url in urls:
                    db.execute('INSERT INTO account_targets(id,account_id,url) VALUES (?,?,?) '
                               'ON CONFLICT(account_id,url) DO UPDATE SET enabled=1', (uid(), identity, url))
                for url in previous_urls - set(urls):
                    self._archive_target_submissions(db, identity, url)
            self.audit(db, 'account_saved', identity)
        return self.account(identity)

    def _change_targets(self, account_id, urls=None, target_id=None, replacement=None):
        from .account_urls import bmc_submission_url
        with self.db(True) as db:
            row = db.execute('SELECT * FROM accounts WHERE id=? AND deleted=0', (account_id,)).fetchone()
            if not row:
                raise KeyError(account_id)
            if row['platform'] != 'bmc' or row['archived']:
                raise ValueError('仅可在未归档的 BMC 账号中管理文章；请先恢复账号。')
            targets = [dict(t) for t in db.execute(
                'SELECT * FROM account_targets WHERE account_id=? AND enabled=1 ORDER BY rowid', (account_id,))]
            removed = next((t for t in targets if t['id'] == target_id), None) if target_id else None
            if target_id and not removed:
                raise KeyError(target_id)
            additions = [bmc_submission_url(replacement)] if replacement is not None else (
                [bmc_submission_url(url) for url in urls] if urls is not None else [])
            if replacement is not None and additions[0] == removed['url']:
                return self._account_item(row, db)
            existing = {t['url'] for t in targets}
            if len(additions) != len(set(additions)) or existing.intersection(additions):
                raise ValueError('该文章链接已在账号中追踪；请勿重复添加。')
            if len(targets) - bool(removed) + len(additions) > 30:
                raise ValueError('每个账号最多追踪 30 篇文章。')
            if removed:
                db.execute('UPDATE account_targets SET enabled=0 WHERE id=?', (removed['id'],))
                self._archive_target_submissions(db, account_id, removed['url'])
            for url in additions:
                db.execute('INSERT INTO account_targets(id,account_id,url) VALUES (?,?,?) '
                           'ON CONFLICT(account_id,url) DO UPDATE SET enabled=1,baseline=0,last_error=NULL,error_category=NULL',
                           (uid(), account_id, url))
            config = self.unpack(row['config'])
            config['submission_urls'] = [r[0] for r in db.execute(
                'SELECT url FROM account_targets WHERE account_id=? AND enabled=1 ORDER BY rowid', (account_id,))]
            config['submission_url'] = next(iter(config['submission_urls']), '')
            db.execute('UPDATE accounts SET config=?,revision=revision+1,enabled=CASE WHEN ? THEN enabled ELSE 0 END WHERE id=?',
                       (self.pack(config), bool(config['submission_urls']), account_id))
            self.audit(db, 'target_replaced' if replacement is not None else 'target_removed' if removed else 'targets_added', account_id)
        return self.account(account_id)

    def add_targets(self, account_id, urls):
        if not isinstance(urls, list) or not 1 <= len(urls) <= 30:
            raise ValueError('请添加 1–30 个文章链接。')
        return self._change_targets(account_id, urls=urls)

    def replace_target(self, account_id, target_id, url):
        if url is None:
            raise ValueError('请填写文章链接。')
        return self._change_targets(account_id, target_id=target_id, replacement=url)

    def remove_target(self, account_id, target_id):
        return self._change_targets(account_id, target_id=target_id)

    def delete_account(self, identity):
        if not self.account(identity):
            raise KeyError(identity)
        with self.db(True) as db:
            db.execute('UPDATE accounts SET deleted=1,archived=1,enabled=0,revision=revision+1,config=? WHERE id=?',
                       (self.pack({}),identity))
            db.execute('UPDATE account_targets SET enabled=0 WHERE account_id=?',(identity,))
            db.execute('UPDATE submissions SET archived=1,manual_archived=1 WHERE account_id=?',(identity,))
            db.execute("UPDATE jobs SET status='interrupted',finished_at=?,error='账号已删除。' WHERE account_id=? AND status='queued'",(now(),identity))
            self.audit(db,'account_deleted',identity)

    def queue_job(self, kind='refresh', account_id=None):
        with self.db(True) as db:
            active = db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()
            if active:
                item = dict(active)
                item['reused'] = True
                return item
            identity = uid()
            db.execute('INSERT INTO jobs(id,kind,account_id,status,created_at) VALUES (?,?,?,?,?)',
                       (identity,kind,account_id,'queued',now()))
            self.audit(db, 'job_queued', identity)
            return dict(db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone())

    def submissions(self, archived=False):
        with self.db() as db:
            rows = db.execute('SELECT s.*,a.name account_name,a.platform FROM submissions s JOIN accounts a ON a.id=s.account_id '
                              'WHERE s.archived=? ORDER BY s.last_success_at DESC', (int(archived),)).fetchall()
        return [self._submission_item(r) for r in rows]

    def submission(self, identity):
        with self.db() as db:
            row = db.execute('SELECT s.*,a.name account_name,a.platform FROM submissions s JOIN accounts a ON a.id=s.account_id WHERE s.id=?', (identity,)).fetchone()
            events = db.execute('SELECT * FROM events WHERE submission_id=? ORDER BY created_at,rowid', (identity,)).fetchall()
        if not row:
            return None
        return {'submission':self._submission_item(row),
                'events':[{**dict(e),'payload':json.loads(e['payload'])} for e in events]}

    def enqueue_notification(self, db, event_id, payload, cfg=None, only=None):
        cfg = cfg or self.settings(db)
        channels = []
        if cfg['pushplus_enabled'] and cfg['pushplus_token']:
            channels.append('pushplus')
        if only:
            channels = [c for c in channels if c == only]
        for channel in channels:
            db.execute('INSERT OR IGNORE INTO notifications(id,channel,status,next_attempt_at,created_at,payload,event_id) '
                       'VALUES (?,?,?,?,?,?,?)', (uid(),channel,'pending',now(),now(),json.dumps(payload,ensure_ascii=False),event_id))
        return channels

    def record_failure(self, account, category, message, cfg):
        with self.db(True) as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (account['id'],)).fetchone()
            if not row or row['deleted'] or row['revision'] != account.get('revision', 0):
                return
            failures = row['failures'] + 1
            alerted = row['alerted']
            if not alerted and (category in ('authentication', 'challenge') or failures >= 2):
                self.enqueue_notification(db, uid(), {'subject':'journalcheck 检查需要处理',
                    'body':f"账号：{account['name']}\n{message}"}, cfg)
                alerted = 1
            db.execute('UPDATE accounts SET last_attempt_at=?,last_error=?,error_category=?,failures=?,alerted=? WHERE id=?',
                       (now(),message,category,failures,alerted,account['id']))

    def apply_rows(self, account, rows, cfg, verified=False, target_results=None):
        timestamp = now()
        fields = ('status','status_date','reviewer_invited','reviewer_accepted','review_reports_received','review_comments')
        changes = 0
        seen = set()
        with self.db(True) as db:
            state = db.execute('SELECT * FROM accounts WHERE id=?', (account['id'],)).fetchone()
            if not state or state['deleted'] or state['revision'] != account.get('revision', 0):
                return 0
            baseline = bool(state['baseline'])
            targets = {r['url']:dict(r) for r in db.execute('SELECT * FROM account_targets WHERE account_id=?',(account['id'],))}
            successful_urls = {r['url'] for r in target_results or [] if r.get('ok')}
            complete = not target_results or all(r.get('ok') for r in target_results)
            for data in rows:
                data = dict(data)
                tracking_url = data.get('metadata',{}).get('tracking_url',data.get('detail_url'))
                if tracking_url in targets and not targets[tracking_url]['enabled']:
                    continue
                number = str(data.get('manuscript_number') or '').strip()
                if not number or not str(data.get('status') or '').strip():
                    raise ValueError('页面缺少稿件编号或状态。')
                if number in seen:
                    continue
                seen.add(number)
                old = db.execute('SELECT * FROM submissions WHERE account_id=? AND manuscript_number=?', (account['id'],number)).fetchone()
                previous = json.loads(old['payload']) if old else None
                # Keep a stable observed start; site dates take precedence when supplied.
                status_changed = bool(old and previous.get('status') != data.get('status'))
                supplied_date = data.get('status_date')
                if old and not status_changed and not supplied_date:
                    data['status_date'] = previous.get('status_date')
                if supplied_date:
                    since, source = normalized_date(supplied_date) or supplied_date, 'platform'
                elif old and not status_changed:
                    since, source = old['status_since'], old['status_since_source']
                else:
                    since, source = timestamp, 'observed'
                changed_fields = [f for f in fields if field_changed(f,previous,data)]
                # Compare observed values, retaining prior counts only as explicitly stale display values.
                meta = dict(data.get('metadata') or {})
                meta.pop('stale_counts',None)
                stale = []
                if old:
                    for name in ('reviewer_invited','reviewer_accepted','review_reports_received'):
                        if data.get(name) is None and previous.get(name) is not None:
                            meta.setdefault('last_known_counts',{})[name] = previous[name]
                            stale.append(name)
                        elif data.get(name) is None and name in previous.get('metadata',{}).get('last_known_counts',{}):
                            meta.setdefault('last_known_counts',{})[name] = previous['metadata']['last_known_counts'][name]
                            stale.append(name)
                if stale:
                    meta['stale_counts'] = stale
                data['metadata'] = meta
                identity = old['id'] if old else uid()
                archived = int(is_terminal(data['status']) or bool(old and old['manual_archived']))
                db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,manuscript_number) DO UPDATE SET '
                           'payload=excluded.payload,last_success_at=excluded.last_success_at,status_since=excluded.status_since,'
                           'status_since_source=excluded.status_since_source,missing=0,archived=excluded.archived',
                           (identity,account['id'],number,json.dumps(data,ensure_ascii=False),old['first_seen_at'] if old else timestamp,
                            timestamp,since,source,0,archived,old['manual_archived'] if old else 0))
                if changed_fields:
                    event_id = uid()
                    tracking_url = data.get('metadata',{}).get('tracking_url',data.get('detail_url'))
                    silent_new = tracking_url in targets and not targets[tracking_url]['baseline']
                    kind = 'baseline' if silent_new else 'changed' if old else 'baseline' if not baseline else 'new_submission'
                    payload = {'fields':changed_fields,'before':previous,'after':data}
                    db.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                               (event_id,identity,timestamp,kind,json.dumps(payload,ensure_ascii=False)))
                    if baseline and not silent_new:
                        changes += 1
                        self.enqueue_notification(db,event_id,
                            submission_notification(account,previous,data,changed_fields),cfg)
            for old in db.execute('SELECT * FROM submissions WHERE account_id=? AND archived=0', (account['id'],)).fetchall():
                old_data = json.loads(old['payload'])
                old_url = old_data.get('metadata',{}).get('tracking_url',old_data.get('detail_url'))
                if target_results is not None and old_url not in successful_urls:
                    continue
                if old['manuscript_number'] not in seen and not old['missing']:
                    db.execute('UPDATE submissions SET missing=1 WHERE id=?', (old['id'],))
                    db.execute('INSERT INTO events VALUES (?,?,?,?,?)', (uid(),old['id'],timestamp,'missing',
                               json.dumps({'description':'稿件暂未在有效列表中找到，保留最后有效记录。'},ensure_ascii=False)))
            for result in target_results or []:
                if result.get('ok'):
                    db.execute('UPDATE account_targets SET baseline=1,last_attempt_at=?,last_success_at=?,last_error=NULL,error_category=NULL WHERE account_id=? AND url=? AND enabled=1',
                               (timestamp,timestamp,account['id'],result['url']))
                else:
                    db.execute('UPDATE account_targets SET last_attempt_at=?,last_error=?,error_category=? WHERE account_id=? AND url=? AND enabled=1',
                               (timestamp,result.get('message','稿件检查失败，保留旧状态。'),result.get('category','internal'),account['id'],result['url']))
            if not complete:
                # Successful targets are stored; a failed target never loses its last valid status.
                return changes
            if state['alerted']:
                self.enqueue_notification(db,uid(),{'subject':'journalcheck 检查恢复',
                                                   'body':f"账号 {account['name']} 已恢复正常检查。"},cfg)
            db.execute('UPDATE accounts SET baseline=1,last_attempt_at=?,last_success_at=?,last_error=NULL,error_category=NULL,'
                       'failures=0,alerted=0,enabled=CASE WHEN ? AND archived=0 AND revision=? THEN 1 ELSE enabled END WHERE id=?',
                       (timestamp,timestamp,int(verified),account.get('revision',0),account['id']))
        return changes

    def backup(self):
        directory = self.root / 'backups'
        directory.mkdir(mode=0o700,exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        target = directory / f'journalcheck-{stamp}.sqlite'
        with self.db() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        target.chmod(0o600)
        for path in sorted(directory.glob('journalcheck-*.sqlite'),reverse=True)[7:]:
            path.unlink()
        self.set_meta('last_backup_at',now())
        return target

    def bootstrap(self, allowed_login):
        if self.meta('admin_password'):
            if self.meta('allowed_login') != allowed_login:
                raise ValueError('已保存的 Tailscale 身份不匹配；请检查部署配置。')
            return False
        password = secrets.token_urlsafe(24)
        private_write(self.root / 'initial-password.txt', (password+'\n').encode())
        self.set_meta('admin_password',hash_password(password))
        self.set_meta('must_change_password','1')
        self.set_meta('allowed_login',allowed_login)
        return True
