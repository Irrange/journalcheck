"""Bounded manual ScholarOne sessions. No passwords, cookies or screenshots in APIs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from .browser_profiles import (BrowserBusyError, profile_path, profile_lock,
                               secure_profile, tools_ready, virtual_display,
                               save_browser_state, restore_browser_state)
from .security import private_write
from .store import Store, later, now, uid

LIVE = {'starting', 'active'}
TIME_LIMIT = 900


def write_json(path, values):
    temporary = path.with_name(path.name + '.' + uid() + '.new')
    private_write(temporary, json.dumps(values, ensure_ascii=False).encode())
    temporary.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


class BrowserSessions:
    def __init__(self, store):
        self.store = store
        self.directory = store.root.resolve() / 'browser-sessions'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        self.processes = {}
        self.lock = threading.RLock()
        # A web restart must eventually remove browser credentials of deleted accounts,
        # including cleanup deferred while a previous worker still held the lock.
        profile_root = store.root / 'browser-profiles'
        if profile_root.exists():
            for path in profile_root.iterdir():
                if re.fullmatch(r'[a-f0-9]{32}', path.name) and not store.account(path.name):
                    self.delete_account(path.name)

    def folder(self, identity):
        if not re.fullmatch(r'[a-f0-9]{32}', identity):
            raise KeyError(identity)
        return self.directory / identity

    def _account(self, identity, active=False):
        account = self.store.account(identity, private=True)
        if not account:
            raise KeyError(identity)
        if account['platform'] != 'scholarone':
            raise ValueError('此登录入口仅用于 ScholarOne。')
        if active and account['archived']:
            raise ValueError('请先恢复账号，再更新登录会话。')
        return account

    def state(self, identity):
        path = self.folder(identity) / 'state.json'
        data = read_json(path)
        if not data:
            raise KeyError(identity)
        account = self._account(data['account_id'])
        if data['status'] in LIVE:
            proc = self.processes.get(identity)
            if proc is None or proc.poll() is not None:
                data.update(status='interrupted', message='浏览器会话已中断，请重新打开登录入口。')
                write_json(path, data)
                (path.parent / 'snapshot').unlink(missing_ok=True)
            elif datetime.fromisoformat(data['expires_at']) <= datetime.now(timezone.utc):
                self.command(identity, 'cancel')
                data.update(status='expired', message='登录窗口已超过 15 分钟，请重新打开。')
                write_json(path, data)
            elif account['archived']:
                self.command(identity, 'cancel')
                data.update(status='cancelled', message='账号已归档，登录窗口已停止。')
                write_json(path, data)
        if data['status'] == 'saved':
            saved = read_json(self.folder(identity) / 'profile.json')
            if saved.get('version') != profile_path(self.store.root, account).name:
                data.update(status='expired', message='登录资料已变更，请重新更新会话。')
        return self.public(data)

    @staticmethod
    def public(data):
        return {**{key:data.get(key) for key in
            ('id','account_id','status','message','expires_at')},
            'url':f"/journal/browser-login/{data['id']}"}

    def current(self, account_id):
        account = self._account(account_id)
        files = sorted(self.directory.glob('*/state.json'), key=lambda path:path.stat().st_mtime, reverse=True)
        for path in files:
            if read_json(path).get('account_id') == account_id:
                state = self.state(path.parent.name)
                if state['status'] in ('cancelled','interrupted','expired') and account.get('last_success_at'):
                    profile = profile_path(self.store.root, account)
                    if (profile / 'state.fernet').exists() and (profile / 'session.json').exists():
                        return {**state,'status':'saved','message':'浏览器会话已保存；是否仍有效以下次检查为准。'}
                return state
        profile = profile_path(self.store.root, account)
        if (profile / 'state.fernet').exists() and (profile / 'session.json').exists():
            return {'status':'saved', 'message':'自动登录会话已保存；后续检查会复用，失效后尝试自动重新登录。'}
        return {'status':'idle', 'message':'检查时会使用已保存账号自动登录，无需预先手动保存会话。'}

    def start(self, account_id):
        with self.lock:
            account = self._account(account_id, active=True)
            for identity in list(self.processes):
                state = self.state(identity)
                if state['status'] in LIVE:
                    if state['account_id'] == account_id:
                        return state
                    raise ValueError('另一账号正在手动登录，请先保存或关闭它的登录窗口。')
                proc = self.processes.pop(identity)
                if proc.poll() is None:
                    proc.wait(timeout=5)
            if not tools_ready(self.store.root):
                raise ValueError('服务器浏览器组件尚未安装，请运行部署说明中的浏览器安装脚本。')
            profile = profile_path(self.store.root, account)
            with profile_lock(profile):
                pass
            identity = uid()
            folder = self.folder(identity)
            folder.mkdir(mode=0o700)
            data = dict(id=identity, account_id=account_id, status='starting',
                        message='正在启动私有浏览器。', expires_at=later(TIME_LIMIT))
            write_json(folder / 'state.json', data)
            # Snapshot encrypted with the existing Fernet key; never pass secrets in argv.
            private_write(folder / 'snapshot', self.store.pack({
                'account':account, 'settings':self.store.settings()}).encode())
            self.processes[identity] = subprocess.Popen([sys.executable,
                '-m','journalcheck.server.browser_sessions', str(self.store.root.resolve()), identity],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
            with self.store.db() as db:
                self.store.audit(db, 'browser_session_started', account_id)
            return self.public(data)

    def command(self, identity, action):
        if action not in ('fill', 'save', 'cancel'):
            raise ValueError('浏览器操作无效。')
        folder = self.folder(identity)
        state = read_json(folder / 'state.json')
        if not state:
            raise KeyError(identity)
        if state['status'] not in LIVE:
            raise ValueError('登录窗口已结束，请重新打开。')
        write_json(folder / 'command.json', {'action':action,'id':uid()})
        return self.public(state)

    def socket(self, identity):
        data = self.state(identity)
        if data['status'] != 'active':
            raise ValueError('登录窗口尚未就绪或已结束。')
        return self.folder(identity) / 'vnc.sock'

    def delete_account(self, account_id):
        with self.lock:
            for identity, proc in list(self.processes.items()):
                data = read_json(self.folder(identity) / 'state.json')
                if data.get('account_id') != account_id:
                    continue
                self.stop_process(proc)
                self.processes.pop(identity)
                data.update(status='cancelled', message='账号已删除，浏览器会话已清除。')
                write_json(self.folder(identity) / 'state.json', data)
            profile = self.store.root / 'browser-profiles' / account_id
            # Worker may still hold this account's lock. Credentials are already
            # deleted in SQLite; remove the private on-disk profile after it releases.
            def remove_profile(blocking=False):
                lock_file = profile / 'profile.lock'
                if lock_file.exists():
                    import fcntl
                    try:
                        with lock_file.open('rb') as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                            shutil.rmtree(profile, ignore_errors=True)
                    except FileNotFoundError:
                        pass
                    except BlockingIOError:
                        threading.Thread(target=remove_profile, args=(True,), daemon=True).start()
                else:
                    shutil.rmtree(profile, ignore_errors=True)
            remove_profile()

    @staticmethod
    def stop_process(proc):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            except ProcessLookupError:
                pass

    def close(self):
        for proc in self.processes.values():
            self.stop_process(proc)


def run_session(root, identity):
    from playwright.sync_api import sync_playwright
    from .adapters import AuthenticationError, ChallengeError
    from .orcid import browser_proxy
    from .scholarone import authenticated_page, fill_login_form
    os.umask(0o077)
    store = Store(Path(root))
    folder = Path(root) / 'browser-sessions' / identity
    data = read_json(folder / 'state.json')
    snapshot = store.unpack((folder / 'snapshot').read_text())
    (folder / 'snapshot').unlink(missing_ok=True)
    account, network = snapshot['account'], snapshot['settings']
    profile = profile_path(root, account)
    def update(status, message):
        data.update(status=status, message=message)
        write_json(folder / 'state.json', data)
    try:
        with profile_lock(profile), virtual_display(root, folder) as display, sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(str(profile), headless=False,
                env={**os.environ, **display}, proxy=browser_proxy(network),
                viewport={'width':1280,'height':720},
                args=['--no-sandbox','--window-size=1280,800','--window-position=0,0'])
            try:
                restore_browser_state(context, profile, root)
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(5000)
                try:
                    page.goto(account['base_url'], wait_until='domcontentloaded', timeout=45000)
                    message = '请完成官方安全验证和登录，然后点击“保存会话”。'
                    from .scholarone import check_page
                    try:
                        check_page(page.content())
                    except ChallengeError:
                        message = '官方页面要求安全验证：请先在浏览器完成验证，再登录并保存会话。'
                    except AuthenticationError:
                        message = '请在官方页面登录；可点击“填入已保存账号”，登录成功后保存会话。'
                except Exception:
                    message = '页面尚未加载完成，可以在浏览器重试；完成登录后保存会话。'
                update('active', message)
                previous = None
                while datetime.now(timezone.utc) < datetime.fromisoformat(data['expires_at']):
                    current = store.account(account['id'], private=True)
                    if not current or current['archived'] or profile_path(root, current) != profile:
                        update('cancelled', '账号资料已变更，登录窗口已停止；请重新打开。'); break
                    if page.is_closed():
                        update('cancelled', '浏览器窗口已关闭。'); break
                    command = read_json(folder / 'command.json')
                    if command.get('id') and command['id'] != previous:
                        previous = command['id']
                        action = command['action']
                        if action == 'cancel':
                            update('cancelled', '登录窗口已关闭，已保存的有效会话保留。'); break
                        try:
                            if action == 'fill':
                                fill_login_form(page, account)
                                update('active', '已在官方登录表单填入账号；请在浏览器点击登录。')
                            elif action == 'save':
                                authenticated_page(page, account)
                                save_browser_state(context, profile, root)
                                # Flush cookies by closing the context BEFORE announcing success.
                                context.close()
                                secure_profile(profile)
                                write_json(folder / 'profile.json', {'version':profile.name})
                                write_json(profile / 'session.json', {'saved_at':now()})
                                with store.db() as db:
                                    store.audit(db, 'browser_session_saved', account['id'])
                                update('saved', '会话已保存，请返回账号管理并“验证并启用”。'); break
                        except ChallengeError:
                            update('active', '请先在浏览器完成官方安全验证，再登录并保存会话。')
                        except AuthenticationError:
                            update('active', '尚未确认登录，请进入官方主页或作者中心后再保存。')
                        except Exception:
                            update('active', '操作未完成，请检查浏览器页面后重试。')
                    page.wait_for_timeout(500)
                else:
                    update('expired', '登录窗口已超过 15 分钟，请重新打开。')
            finally:
                context.close()
    except BrowserBusyError:
        update('failed', '此账号有后台检查正在运行，请稍后重新打开。')
    except Exception:
        update('failed', '私有浏览器未能启动或已中断，请重试或检查服务健康。')
    finally:
        secure_profile(profile)
        (folder / 'snapshot').unlink(missing_ok=True)


if __name__ == '__main__':
    run_session(sys.argv[1], sys.argv[2])
