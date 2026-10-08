"""Private, account-specific browser profiles shared by manual login and polling."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time


class BrowserBusyError(RuntimeError):
    pass


def profile_path(root, account):
    identity = account.get('id', '')
    if not re.fullmatch(r'[a-f0-9]{32}', identity):
        raise ValueError('浏览器会话需要已保存的账号。')
    # Credentials changes select a fresh profile; names and monitoring flags do not.
    material = json.dumps([account.get(k, '') for k in
                           ('base_url', 'username', 'password')], ensure_ascii=False)
    version = hashlib.sha256(material.encode()).hexdigest()[:32]
    path = Path(root) / 'browser-profiles' / identity / version
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    for directory in (path, path.parent, path.parent.parent):
        directory.chmod(0o700)
    return path


@contextmanager
def profile_lock(path):
    fd = os.open(Path(path).parent / 'profile.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BrowserBusyError('此账号正在更新登录会话，请稍后检查。') from None
        yield
    finally:
        os.close(fd)


def tools_path(root):
    return Path(os.environ.get('JOURNALCHECK_BROWSER_TOOLS', Path(root) / 'browser-tools'))


def tools_ready(root):
    tools = tools_path(root)
    return (tools / 'root/usr/bin/Xtigervnc').is_file() and (tools / 'novnc/core/rfb.js').is_file()


@contextmanager
def virtual_display(root, directory):
    """No public VNC/TCP port: RFB uses a private Unix socket and X11 uses xauth."""
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    tools = tools_path(root)
    binary = tools / 'root/usr/bin/Xtigervnc'
    env = {**os.environ, 'LD_LIBRARY_PATH': str(tools / 'root/usr/lib/x86_64-linux-gnu'),
           'PATH': str(tools / 'root/usr/bin') + os.pathsep + os.environ.get('PATH', '')}
    sock = directory / 'vnc.sock'
    authority = directory / 'Xauthority'
    # Unix socket filenames must fit the platform's 108-byte limit.
    if len(os.fsencode(sock)) >= 104:
        raise ValueError('浏览器运行目录过长。')
    for _ in range(20):
        number = secrets.randbelow(9000) + 1000
        if not Path(f'/tmp/.X11-unix/X{number}').exists() and not Path(f'/tmp/.X{number}-lock').exists():
            break
    else:
        raise RuntimeError('没有可用的浏览器显示。')
    authority.touch(mode=0o600)
    authority.chmod(0o600)
    subprocess.run(['xauth', '-f', str(authority), 'add', f':{number}', '.', secrets.token_hex(16)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sock.unlink(missing_ok=True)
    proc = subprocess.Popen([str(binary), f':{number}', '-geometry', '1280x800', '-depth', '24',
        '-auth', str(authority), '-nolisten', 'tcp', '-rfbport', '-1',
        '-rfbunixpath', str(sock), '-rfbunixmode', '0600', '-SecurityTypes', 'None',
        '-AlwaysShared', '-desktop', 'Journalcheck login'], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError('私有浏览器显示启动失败。')
            if sock.exists() and Path(f'/tmp/.X11-unix/X{number}').exists():
                break
            time.sleep(.1)
        else:
            raise RuntimeError('私有浏览器显示启动超时。')
        sock.chmod(0o600)
        yield {'DISPLAY': f':{number}', 'XAUTHORITY': str(authority)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
        sock.unlink(missing_ok=True)


def secure_profile(path):
    for current, directories, files in os.walk(path, followlinks=False):
        Path(current).chmod(0o700)
        for filename in files:
            candidate = Path(current) / filename
            if candidate.is_file() and not candidate.is_symlink():
                candidate.chmod(0o600)


def save_browser_state(context, profile, root):
    """Chromium drops session cookies on clean exit: encrypt a private restoration copy."""
    from .security import Secrets, private_write
    payload = json.dumps(context.storage_state(), ensure_ascii=False)
    target = Path(profile) / 'state.fernet'
    temporary = target.with_suffix('.new')
    private_write(temporary, Secrets(Path(root)).encrypt(payload).encode())
    temporary.replace(target)
    from .store import now
    private_write(Path(profile) / 'session.json', json.dumps({'saved_at':now()}).encode())


def restore_browser_state(context, profile, root):
    from cryptography.fernet import InvalidToken
    from .security import Secrets
    path = Path(profile) / 'state.fernet'
    if not path.exists():
        return
    try:
        state = json.loads(Secrets(Path(root)).decrypt(path.read_text()))
    except (InvalidToken, ValueError):
        # A damaged optional browser cache must not prevent manual re-login.
        return
    cookies = [cookie for cookie in state.get('cookies', [])
               if cookie.get('expires', -1) == -1 or cookie.get('expires', 0) > time.time()]
    if cookies:
        context.add_cookies(cookies)
    origins = json.dumps(state.get('origins', []), ensure_ascii=False)
    context.add_init_script('''(() => {
        const origins = ''' + origins + ''';
        for (const item of origins) if (item.origin === location.origin) {
            for (const value of item.localStorage || []) {
                if (localStorage.getItem(value.name) === null) localStorage.setItem(value.name, value.value);
            }
        }
    })();''')
