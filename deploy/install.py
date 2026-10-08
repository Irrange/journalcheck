#!/usr/bin/env python3
"""Install the two user services and one private Tailscale route. No root needed."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

REPO=Path(__file__).resolve().parents[1]
RUNTIME=REPO/'.runtime'
TAILSCALE=Path.home()/'opt/tailscale/tailscale'
SOCKET=Path.home()/'.tailscale/tailscaled.sock'
UNITS=('journalcheck-web.service','journalcheck-worker.service')


def run(args, capture=False):
    return subprocess.run([str(v) for v in args],check=True,text=True,capture_output=capture)


def ts(*args):
    return [TAILSCALE,f'--socket={SOCKET}',*args]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    os.umask(0o077)
    python=REPO/'.venv/bin/python'
    # Reinstalling standalone units would erase the signed portal gateway setup.
    unitdir=Path.home()/'.config/systemd/user'
    web_unit=unitdir/UNITS[0]
    if web_unit.exists() and 'JOURNALCHECK_GATEWAY' in web_unit.read_text():
        parser.error('现有服务使用门户认证；升级请用 deploy/upgrade.py。路由回滚须在门户配置中处理。')
    if args.rollback:
        if args.dry_run:
            print('将停止并禁用 journalcheck 的两个用户服务，仅移除 /journal 路由；保留数据。')
            return
        run(['systemctl','--user','disable','--now',*UNITS])
        run(ts('serve','--https=443','--set-path=/journal','off'))
        print('已停止 journalcheck 服务并移除其路由；运行数据已保留。')
        return
    if not python.exists():
        parser.error('请先创建项目虚拟环境并安装 requirements-server.lock。')
    status=json.loads(run(ts('status','--json'),True).stdout)
    if status.get('BackendState')!='Running':
        parser.error('Tailscale 未运行。')
    owner=status.get('User',{}).get(str(status['Self']['UserID']),{}).get('LoginName')
    host=status['Self']['DNSName'].rstrip('.')
    if not owner or status['Self'].get('Tags'):
        parser.error('无法确认当前节点的个人 Tailscale 身份。')
    serve=json.loads(run(ts('serve','status','--json'),True).stdout)
    handlers=serve.get('Web',{}).get(host+':443',{}).get('Handlers',{})
    existing=handlers.get('/journal') or handlers.get('/journal/')
    if existing and existing.get('Proxy')!='http://127.0.0.1:18181':
        parser.error('/journal 已被另一个服务使用，停止部署。')
    if args.dry_run:
        print('检查通过：将安装 journalcheck-web / journalcheck-worker 用户服务，新增 /journal 私有 HTTPS 路由。')
        return
    RUNTIME.mkdir(mode=0o700,exist_ok=True)
    RUNTIME.chmod(0o700)
    snapshot=RUNTIME/'serve-before.json'
    if not snapshot.exists():
        snapshot.write_text(json.dumps(serve,ensure_ascii=False,indent=2))
        snapshot.chmod(0o600)
    run([python,'-m','journalcheck.server','init','--runtime',RUNTIME,'--allowed-login',owner,'--origin','https://'+host])
    if (RUNTIME/'journalcheck.sqlite').exists():
        run([python,'-m','journalcheck.server','backup','--runtime',RUNTIME])
    unitdir=Path.home()/'.config/systemd/user'
    unitdir.mkdir(parents=True,exist_ok=True)
    for mode,name in zip(('web','worker'),UNITS):
        text=f'''[Unit]
Description=Journalcheck {mode} (private Tailscale service)
After=network-online.target tailscaled-user.service

[Service]
Type=simple
WorkingDirectory={REPO}
Environment=JOURNALCHECK_RUNTIME={RUNTIME}
Environment=PYTHONUNBUFFERED=1
ExecStart={python} -m journalcheck.server {mode}
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillMode=control-group
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
'''
        (unitdir/name).write_text(text)
    run(['systemctl','--user','daemon-reload'])
    run(['systemctl','--user','enable','--now',*UNITS])
    run(['systemctl','--user','restart',*UNITS])
    # Wait for systemd to report startup failure before adding the route.
    import time
    time.sleep(2)
    run(['systemctl','--user','is-active',*UNITS])
    run(ts('serve','--bg','--https=443','--set-path=/journal','http://127.0.0.1:18181'))
    after=json.loads(run(ts('serve','status','--json'),True).stdout)
    original=json.loads(json.dumps(serve))
    for config in (after,original):
        for web in config.get('Web',{}).values():
            web.get('Handlers',{}).pop('/journal',None)
            web.get('Handlers',{}).pop('/journal/',None)
    if after!=original:
        raise RuntimeError('检测到其他 Tailscale 配置变化，请查看保留的部署前快照。')
    print('部署完成：https://'+host+'/journal/')
    print('初始密码文件位于项目私有运行目录；请通过 SSH 读取，不要提交到 Git。')


if __name__=='__main__':
    main()
