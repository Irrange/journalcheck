from __future__ import annotations
import argparse
import os
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description='journalcheck 私有网页服务')
    parser.add_argument('command',choices=['web','worker','init','backup','reset-password'])
    parser.add_argument('--runtime',default=os.environ.get('JOURNALCHECK_RUNTIME','.runtime'))
    parser.add_argument('--allowed-login')
    parser.add_argument('--origin')
    parser.add_argument('--port',type=int,default=18181)
    args=parser.parse_args()
    os.umask(0o077)
    runtime=Path(args.runtime).resolve()
    os.environ['JOURNALCHECK_RUNTIME']=str(runtime)
    from .store import Store
    store=Store(runtime)
    if args.command=='init':
        if not args.allowed_login or not args.origin:
            parser.error('初始化需要 --allowed-login 和 --origin。')
        store.bootstrap(args.allowed_login)
        store.set_meta('public_origin',args.origin.rstrip('/'))
        print('初始化完成；初始密码保存在私有运行目录的 initial-password.txt。')
    elif args.command=='backup':
        store.backup()
        print('备份完成。')
    elif args.command=='reset-password':
        import secrets
        from .security import hash_password,private_write
        password=secrets.token_urlsafe(24)
        with store.db() as db:
            db.execute("UPDATE meta SET value=? WHERE key='admin_password'",(hash_password(password),))
            db.execute("UPDATE meta SET value='1' WHERE key='must_change_password'")
            db.execute('DELETE FROM sessions')
        private_write(runtime/'initial-password.txt',(password+'\n').encode())
        print('密码已重置；请通过 SSH 读取私有初始密码文件。')
    elif args.command=='web':
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(runtime),host='127.0.0.1',port=args.port,workers=1,access_log=False,proxy_headers=False)
    else:
        from .worker import Worker
        Worker(store).run()


if __name__=='__main__':
    main()
