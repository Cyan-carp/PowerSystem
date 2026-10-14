"""SEC-M4 actual dedicated chain recovery and negative-boundary checks; no writes."""
import argparse, asyncio, json, os, subprocess, sys, hashlib
from pathlib import Path
from datetime import datetime, timezone
from uuid import UUID, uuid4
import httpx, asyncpg

ROOT=Path(__file__).resolve().parents[3]
for name in ('09-ops-agent','10-security-agent','08-agent-rag'):sys.path.insert(0,str(ROOT/'stages'/name))
from ops_agent.config import Config
from ops_agent.store import Store
from ops_agent.schemas import Task
from review_contract import digest, sign_message

def read_env(p):return dict(line.split('=',1) for line in p.read_text(encoding='utf-8-sig').splitlines() if '=' in line)

async def main(a):
    run=(ROOT/a.run).resolve();out=(ROOT/a.output).resolve()
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent') or not out.is_relative_to(run):raise ValueError('isolated_output_required')
    out.mkdir(exist_ok=False);env=read_env(run/'private/local.env');os.environ.update(env)
    cfg=Config.load();checks={}
    if a.phase=='fixture-cancel':
        store=await Store.open(cfg)
        t=Task(protocol='ma/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),actor={'user_id':1,'role':'admin'},task_kind='security_inspection',intent='SEC-M4取消页面fixture：零远端采集、零模型、零写',user_request='巡检本项目（取消页面fixture）',created_at=datetime.now(timezone.utc))
        tid,_=await store.create(t);row=await store.claim(tid,'sec-m4-cancel-ui-fixture')
        result={'tool_calls':[],'sources':[],'warnings':['明确合成取消页面fixture；没有远端采集或模型调用。'],'diagnosis':{'summary':'等待本人取消的合成页面fixture；取消后保留这段程序说明。'},'inspection_report':{'version':'sec-m3-report/v1','requested_scope':[],'checked_scope':[],'items':[],'confirmed_findings':[],'hypotheses':[],'unchecked_items':[],'recommendations':[],'completeness':'limited_project_checks','review_status':'not_started','created_at':datetime.now(timezone.utc).isoformat()}}
        await store.transition(tid,row['execution_epoch'],'executing',result)
        from rag_agent.store import Store as RagStore
        history=await RagStore.open(env['RAG_DATABASE_URL'])
        report=await store.report(tid,1)
        await history.ensure(t.session_id,1,'SEC-M4取消页面fixture（零模型、零采集）',True)
        await history.save(t.session_id,t.user_request,{'status':'answered','answer_markdown':'明确合成取消页面fixture，等待点击任务卡取消；零模型、零远端采集、零写。','sources':[],'tool_calls':[],'warnings':[],'model':'program-fixture','model_called':False,'response_mode':'task_dispatched','request_id':str(uuid4()),'task_cards':[report]},[])
        await history.pool.close()
        (out/'ready.json').write_text(json.dumps({'task_id':str(tid),'fixture':True,'model_calls':0,'remote_reads':0}),encoding='utf-8')
        try:
            for _ in range(150):
                current=await store.get(tid)
                if current['cancel_requested']:
                    await store.transition(tid,row['execution_epoch'],'cancelled',result,'user_cancel_fixture');break
                await store.heartbeat(tid,row['execution_epoch']);await asyncio.sleep(2)
            else:await store.transition(tid,row['execution_epoch'],'timeout',result,'fixture_deadline')
            report=await store.report(tid,1);(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,default=str,indent=2),encoding='utf-8')
            checks={'cancelled':report['status']=='cancelled','zero_calls':not report['tool_calls'] and report['model_calls']==0,'zero_write':not report['execution_attempts']}
        finally:await store.pool.close()
    else:
        async with httpx.AsyncClient(trust_env=False,timeout=10) as c:
            import time
            until=time.monotonic()+60
            while True:
                try:
                    ready=(await c.get(cfg.backend_url+'/api/v1/ping',timeout=2)).status_code==200 and (await c.get('http://127.0.0.1:8115/health',timeout=2)).status_code==200
                except httpx.HTTPError:ready=False
                if ready:break
                if time.monotonic()>=until:raise ValueError('owned_services_not_ready')
                await asyncio.sleep(1)
            async def login(role):
                r=await c.post(cfg.backend_url+'/api/v1/auth/login',json={'username':'ma_m3_'+role,'password':Path(env['MA_M3_'+role.upper()+'_PASSWORD_FILE']).read_text().strip()});r.raise_for_status();return {'Authorization':'Bearer '+r.json()['data']['token']}
            admin=await login('admin');operator=await login('operator');r=await c.get(cfg.backend_url+'/api/v1/agent/tasks/'+a.task,headers=admin);r.raise_for_status();report=r.json()['data']
            db=await asyncpg.connect(cfg.database_url)
            if a.phase=='before-recovery':
                records=[dict(r) for r in await db.fetch('SELECT review_id,decision_hash,state FROM ma_security_reviews ORDER BY review_id')]
                (out/'baseline.json').write_text(json.dumps({'task':report,'reviews':records,'attempts':await db.fetchval('SELECT count(*) FROM ma_execution_attempts')},ensure_ascii=False,default=str,indent=2),encoding='utf-8')
                checks={'report_saved':bool(report['inspection_report']),'zero_write':not report['execution_attempts']}
            elif a.phase=='after-recovery':
                baseline=json.loads(Path(a.baseline).read_text(encoding='utf-8'))
                records=[dict(r) for r in await db.fetch('SELECT review_id,decision_hash,state FROM ma_security_reviews ORDER BY review_id')]
                checks={'task_equal':report==baseline['task'],'reviews_equal':json.loads(json.dumps(records,default=str))==baseline['reviews'],'cross_owner_404':(await c.get(cfg.backend_url+'/api/v1/agent/tasks/'+a.task,headers=operator)).status_code==404,'attempts_equal':await db.fetchval('SELECT count(*) FROM ma_execution_attempts')==baseline['attempts']}
            elif a.phase=='closed':
                attempts_before=await db.fetchval('SELECT count(*) FROM ma_execution_attempts')
                broker=json.loads((run/'private/broker/broker.json').read_text());window=json.loads((run/'private/broker/write-window.json').read_text())
                closed=not cfg.write_enabled and broker['write_enabled'] is False and window['write_enabled'] is False
                if not closed:raise ValueError('closed_negative_requires_all_gates_closed')
                from ops_write_contract import snapshot_grant,sign,verify
                import time
                key=Path(broker['write_key_file'].replace('/private',str(run/'private/broker'))).read_text().strip()
                snap=await c.post('http://127.0.0.1:8117/internal/drill/v1/snapshot',json={'grant':snapshot_grant(broker['asset_id'],key)});snap.raise_for_status()
                before=snap.json()['snapshot'];now=int(time.time())
                grant={k:before[k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version')}
                grant.update(aud='ma-drill-executor',kid='ma-write-v1',iat=now,exp=now+30,action='restart',task_id=str(uuid4()),trace_id=str(uuid4()),execution_epoch=1,step_no=1,call_id=str(uuid4()),user_id=999001,role='admin',args_hash=digest({'asset_id':broker['asset_id']}),deadline=now+30,approval_id=str(uuid4()))
                token=sign(grant,key);verify(token,key)
                response=await c.post('http://127.0.0.1:8117/internal/drill/v1/restart',json={'grant':token})
                after=await c.post('http://127.0.0.1:8117/internal/drill/v1/snapshot',json={'grant':snapshot_grant(broker['asset_id'],key)});after.raise_for_status()
                invalid=await c.post(cfg.executor_url+'/internal/ma-exec/v2/writes',headers={'X-MA-Executor-Token':cfg.executor_token},json={'grant':'invalid-fixture-no-execution'})
                checks={'config_closed':closed,'signed_valid_write_disabled':response.status_code==403 and response.json().get('detail')=='write_disabled','container_not_restarted':before['started_at']==after.json()['snapshot']['started_at'],'attempts_equal':await db.fetchval('SELECT count(*) FROM ma_execution_attempts')==attempts_before,'invalid_write_rejected':invalid.status_code==403,'cross_owner_404':(await c.get(cfg.backend_url+'/api/v1/agent/tasks/'+a.task,headers=operator)).status_code==404}

            await db.close()
    (out/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(checks))
    if not all(checks.values()):raise ValueError('m4_verification_failed')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--output',required=True);p.add_argument('--task');p.add_argument('--baseline');p.add_argument('--phase',required=True,choices=('before-recovery','after-recovery','closed','fixture-cancel'));asyncio.run(main(p.parse_args()))
