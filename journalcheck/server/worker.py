from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime,timezone
from pathlib import Path
from .store import Store, now, later

MESSAGES = {'authentication':'登录无法确认，请检查账号、验证码或多因素认证。',
            'challenge':'投稿平台要求 Cloudflare／验证码等安全验证，尚未验证密码；ScholarOne 请在账号管理中点击“登录／更新会话”，完成验证和登录后保存会话。',
            'busy':'此账号正在更新登录会话，本次暂缓检查，已保留有效稿件状态。',
            'access':'稿件链接失效或账号没有访问权限，已保留最后有效数据。',
            'network':'网络连接或响应失败，请检查站点和代理。',
            'parse':'页面结构无法识别，已保留最后有效数据。',
            'timeout':'本次检查超过十分钟，已保留最后有效数据。',
            'internal':'检查未能完成，已保留最后有效数据。',
            'configuration':'检查期间账号配置已变更，本次旧配置结果未保存。'}


def fetch_child():
    from .adapters import fetch_account_result, AuthenticationError, ChallengeError, ParseError
    import requests
    from .browser_profiles import BrowserBusyError
    data = json.load(sys.stdin)
    try:
        result = fetch_account_result(data['account'],data['settings'])
    except BrowserBusyError:
        result = {'ok':False,'category':'busy'}
    except ChallengeError:
        result = {'ok':False,'category':'challenge'}
    except AuthenticationError:
        result = {'ok':False,'category':'authentication'}
    except ParseError:
        result = {'ok':False,'category':'parse'}
    except requests.RequestException:
        result = {'ok':False,'category':'network'}
    except Exception:
        result = {'ok':False,'category':'internal'}
    sys.stdout.write(json.dumps(result,ensure_ascii=False))


def fetch_with_deadline(store, account, cfg, timeout=600):
    cfg = {**cfg, '_runtime_root':str(store.root.resolve())}
    def kill_fetch_tree(proc):
        # ORCID has browser children. Kill only this fetch's new process group.
        if getattr(proc,'pid',None):
            try:
                os.killpg(proc.pid,signal.SIGKILL)
                return
            except ProcessLookupError:
                return
        proc.kill()
    start = time.monotonic()
    for attempt in range(3):
        remaining = timeout-(time.monotonic()-start)
        if remaining<=0:
            return {'ok':False,'category':'timeout'}
        proc = subprocess.Popen([sys.executable,'-m','journalcheck.server.worker','--fetch'],
                                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                text=True,start_new_session=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        # communicate runs on a helper thread so heartbeat remains live during slow sites.
        import concurrent.futures
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(proc.communicate,json.dumps({'account':account,'settings':cfg},ensure_ascii=False))
        output = None
        try:
            while not future.done():
                store.set_meta('worker_seen_at',now())
                if time.monotonic()-start >= timeout:
                    kill_fetch_tree(proc)
                    future.result(timeout=5)
                    return {'ok':False,'category':'timeout'}
                try:
                    output,_ = future.result(timeout=min(15,max(0.1,timeout-(time.monotonic()-start))))
                except concurrent.futures.TimeoutError:
                    pass
            output,_ = future.result()
            result = json.loads(output) if proc.returncode==0 else {'ok':False,'category':'internal'}
        except (ValueError,TypeError):
            result = {'ok':False,'category':'internal'}
        finally:
            if proc.poll() is None:
                kill_fetch_tree(proc)
                proc.wait()
            pool.shutdown(wait=True)
        if result.get('ok') or result.get('category')!='network' or attempt==2:
            return result
        time.sleep(3*(attempt+1))
    return {'ok':False,'category':'internal'}


class Worker:
    def __init__(self,store,fetcher=fetch_with_deadline,sender=None):
        self.store=store
        self.fetcher=fetcher
        if sender is None:
            from .delivery import send
            sender=send
        self.sender=sender

    def recover(self):
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET status='interrupted',finished_at=?,error='服务重启，未完成的任务已中断。' WHERE status='running'",(now(),))
            db.execute("UPDATE notifications SET status='pending',next_attempt_at=?,last_error='服务重启，将重新尝试发送。' WHERE status='sending'",(now(),))
        self.store.set_meta('next_due_at',later(self.store.settings()['interval_minutes']*60))
        self.store.set_meta('worker_seen_at',now())
        if not self.store.meta('observation_started_at'):
            self.store.set_meta('observation_started_at',now())
        self.store.set_meta('worker_starts',int(self.store.meta('worker_starts','0'))+1)

    def run_job(self):
        with self.store.db(True) as db:
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return False
            job=dict(row)
            db.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?",(now(),job['id']))
        cfg=self.store.settings()
        accounts=self.store.accounts(private=True)
        if job['account_id']:
            accounts=[a for a in accounts if a['id']==job['account_id'] and not a['archived'] and (job['kind']=='verify' or a['enabled'])]
        else:
            accounts=[a for a in accounts if a['enabled'] and not a['archived']]
        accounts=[a for a in accounts if a['platform']!='bmc' or a['submission_urls']]
        with self.store.db() as db:
            db.execute('UPDATE jobs SET total=? WHERE id=?',(len(accounts),job['id']))
        results=[]
        try:
            for account in accounts:
                self.store.set_meta('worker_seen_at',now())
                with self.store.db() as db:
                    db.execute('UPDATE accounts SET last_attempt_at=? WHERE id=?',(now(),account['id']))
                try:
                    result=self.fetcher(self.store,account,cfg)
                    current=self.store.account(account['id'])
                    if not current or current['revision'] != account['revision']:
                        result={'ok':False,'category':'configuration'}
                    if result.get('ok'):
                        changes=self.store.apply_rows(account,result['rows'],cfg,verified=job['kind']=='verify',target_results=result.get('targets'))
                        partial=result.get('partial',False)
                        if partial:
                            failed=[r for r in result['targets'] if not r['ok']]
                            category=failed[0].get('category','internal')
                            message=f"{len(failed)} 个稿件链接检查失败，已保存其他链接的有效结果。"+failed[0].get('message','')
                            self.store.record_failure(account,category,message,cfg)
                        results.append({'account_id':account['id'],'account_name':account['name'],'ok':not partial,
                                        'count':len(result['rows']),'changes':changes,'targets':result.get('targets',[]),
                                        **({'category':category,'message':message} if partial else {})})
                    else:
                        category=result.get('category','internal')
                        message=MESSAGES.get(category,MESSAGES['internal'])
                        if category != 'busy':
                            self.store.record_failure(account,category,message,cfg)
                        results.append({'account_id':account['id'],'account_name':account['name'],'ok':False,
                                        'category':category,'message':message})
                except Exception:
                    self.store.record_failure(account,'internal',MESSAGES['internal'],cfg)
                    results.append({'account_id':account['id'],'account_name':account['name'],'ok':False,
                                    'category':'internal','message':MESSAGES['internal']})
                with self.store.db() as db:
                    db.execute('UPDATE jobs SET progress=?,results=? WHERE id=?',
                               (len(results),json.dumps(results,ensure_ascii=False),job['id']))
            state='completed' if all(r['ok'] for r in results) else 'partial' if any(r['ok'] or any(t['ok'] for t in r.get('targets',[])) for r in results) else 'failed'
            with self.store.db() as db:
                db.execute('UPDATE jobs SET status=?,finished_at=? WHERE id=?',(state,now(),job['id']))
        except BaseException:
            with self.store.db() as db:
                db.execute("UPDATE jobs SET status='failed',finished_at=?,error='后台任务中断。' WHERE id=?",(now(),job['id']))
            raise
        self.store.set_meta('next_due_at',later(cfg['interval_minutes']*60))
        return True

    def deliver_one(self):
        with self.store.db(True) as db:
            row=db.execute("SELECT * FROM notifications WHERE status='pending' AND next_attempt_at<=? ORDER BY created_at,rowid LIMIT 1",(now(),)).fetchone()
            if not row:
                return False
            item=dict(row)
            db.execute("UPDATE notifications SET status='sending' WHERE id=?",(item['id'],))
        attempts=item['attempts']+1
        try:
            outcome=self.sender(item['channel'],json.loads(item['payload']),self.store.settings())
            if not isinstance(outcome,dict) or outcome.get('status')!='accepted' or not outcome.get('receipt_id'):
                raise ValueError('Missing PushPlus acceptance receipt')
        except Exception as exc:
            from .delivery import DeliveryError
            delays=(60,300,900,3600)
            message=str(exc) if isinstance(exc,DeliveryError) else '通知发送失败，请检查网络、代理和渠道配置。'
            retry=(not isinstance(exc,DeliveryError) or exc.retryable) and attempts<=4
            with self.store.db() as db:
                db.execute('UPDATE notifications SET status=?,attempts=?,next_attempt_at=?,last_error=? WHERE id=?',
                           ('pending' if retry else 'failed',attempts,later(delays[attempts-1]) if retry else None,message,item['id']))
        else:
            with self.store.db() as db:
                db.execute("UPDATE notifications SET status='accepted',receipt_id=?,attempts=?,next_attempt_at=NULL,last_error=NULL WHERE id=?",
                           (outcome['receipt_id'],attempts,item['id']))
        self.store.set_meta('worker_seen_at',now())
        return True

    def schedule(self):
        cfg=self.store.settings()
        if not cfg['auto_refresh']:
            return
        due=self.store.meta('next_due_at')
        if due and due>now():
            return
        if any(a['enabled'] and not a['archived'] for a in self.store.accounts()):
            self.store.queue_job()
        self.store.set_meta('next_due_at',later(cfg['interval_minutes']*60))

    def tick(self):
        self.store.set_meta('worker_seen_at',now())
        self.schedule()
        last=self.store.meta('last_backup_at')
        if not last or (datetime.now(timezone.utc)-datetime.fromisoformat(last)).total_seconds()>=86400:
            self.store.backup()
        self.deliver_one()
        self.run_job()
        self.store.secure_files()

    def run(self):
        # OS lock prevents two worker processes, including accidental manual launches.
        import fcntl
        lock=open(self.store.root/'worker.lock','a')
        os.chmod(lock.name,0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('已有后台 worker 在运行。')
        self.recover()
        try:
            while True:
                self.tick()
                time.sleep(1)
        finally:
            lock.close()


if __name__=='__main__':
    if '--fetch' in sys.argv:
        fetch_child()
    else:
        os.umask(0o077)
        Worker(Store(Path(os.environ.get('JOURNALCHECK_RUNTIME','.runtime')))).run()
