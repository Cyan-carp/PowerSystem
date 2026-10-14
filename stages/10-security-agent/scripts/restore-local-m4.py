"""Real owned PG dump/restore + readonly app integration; never accesses production."""
import argparse,asyncio,copy,json,os,secrets,subprocess,sys,time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from datetime import datetime,timezone
import asyncpg,httpx
ROOT=Path(__file__).resolve().parents[3]
for name in ('09-ops-agent','10-security-agent'):sys.path.insert(0,str(ROOT/'stages'/name))
from ops_agent.config import Config
from ops_agent.main import create_app
from ops_agent.security import sign
from ops_agent.store import Store
from ops_agent.schemas import Task

async def main(a):
    run=(ROOT/a.run).resolve();out=(ROOT/a.output).resolve()
    if not run.name.startswith('sec-m4-') or not out.is_relative_to(run):raise ValueError('isolated_restore_required')
    out.mkdir(exist_ok=False)
    env=dict(l.split('=',1) for l in (run/'private/local.env').read_text(encoding='utf-8').splitlines() if '=' in l);os.environ.update(env)
    cfg=Config.load()
    if not cfg.database_url.endswith('/sec_m4_acceptance_test') or '127.0.0.1:15435/' not in cfg.database_url:raise ValueError('owned_m4_database_required')
    project=json.loads((run/'drill.json').read_text())['project'];container=project+'-postgres-1'
    dump=out/'acceptance.dump'
    with dump.open('xb') as f:
        p=subprocess.run(['docker','exec',container,'pg_dump','-U','sec_owner','-d','sec_m4_acceptance_test','-Fc'],stdout=f,stderr=subprocess.DEVNULL)
    if p.returncode:raise ValueError('owned_dump_failed')
    name='ma_m4_restore_local_'+str(int(time.time()))
    admin=await asyncpg.connect(cfg.database_url.replace('/sec_m4_acceptance_test','/postgres'))
    await admin.execute('CREATE DATABASE '+name+' OWNER sec_owner');await admin.close()
    subprocess.run(['docker','cp',str(dump),container+':/tmp/'+name+'.dump'],check=True,capture_output=True)
    subprocess.run(['docker','exec',container,'pg_restore','-U','sec_owner','-d',name,'--no-owner','--no-privileges','/tmp/'+name+'.dump'],check=True,capture_output=True)
    owner=await asyncpg.connect(cfg.database_url.replace('/sec_m4_acceptance_test','/'+name))
    tables=await owner.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    counts={r['tablename']:await owner.fetchval('SELECT count(*) FROM '+r['tablename']) for r in tables}
    # Marked recovery-only queued/approved fixture, never a production task or grant.
    seed_cfg=replace(cfg,database_url=cfg.database_url.replace('/sec_m4_acceptance_test','/'+name),runtime_profile='legacy',security_enabled=False,inspection_enabled=False,acceptance_drill_only=False)
    seed=await Store.open(seed_cfg)
    t=Task(protocol='ma/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),actor={'user_id':999001,'role':'admin'},intent='恢复queued/approved fixture：零模型/工具',user_request='恢复fixture禁止执行',created_at=datetime.now(timezone.utc))
    tid,_=await seed.create(t)
    await owner.execute("INSERT INTO ma_task_steps(task_id,step_no,tool,args,args_hash,risk_class,status,execution_epoch) VALUES($1,1,'docker.restart_drill_container','{}',$2,'R2','pending',0)",tid,'a'*64)
    await owner.execute("INSERT INTO ma_policy_versions(version,snapshot) VALUES('restore-fixture','{}') ON CONFLICT DO NOTHING")
    await owner.execute("INSERT INTO ma_approvals(approval_id,task_id,step_no,plan,args_hash,policy_version,status,expires_at) VALUES($1,$2,1,'{}',$3,'restore-fixture','approved',now()+interval '30 minutes')",uuid4(),tid,'a'*64)
    await seed.pool.close()
    before={r['tablename']:await owner.fetchval('SELECT count(*) FROM '+r['tablename']) for r in tables}
    rolestr='ma_restore_'+str(int(time.time()));password=secrets.token_urlsafe(36)
    await owner.execute("CREATE ROLE "+rolestr+" LOGIN PASSWORD '"+password+"' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT")
    await owner.execute('GRANT CONNECT ON DATABASE '+name+' TO '+rolestr)
    await owner.execute('GRANT USAGE ON SCHEMA public TO '+rolestr)
    await owner.execute('GRANT SELECT ON ALL TABLES IN SCHEMA public TO '+rolestr)
    await owner.execute('ALTER ROLE '+rolestr+' SET default_transaction_read_only=on')
    url=cfg.database_url.replace('/sec_m4_acceptance_test','/'+name);from urllib.parse import urlsplit,quote
    parsed=urlsplit(url);url='postgresql://'+rolestr+':'+quote(password,safe='')+'@127.0.0.1:15435/'+name
    restored=replace(cfg,database_url=url,runtime_profile='m4-restore-readonly',enabled=False,write_enabled=False,infra_enabled=False,security_enabled=False,inspection_enabled=False,acceptance_drill_only=False,llm_key='',knowledge_token='',executor_token='',executor_grant_key='',executor_write_key='',security_service_token='',security_request_key='',security_result_key='',redis_prefix='ma:restore:local:',service_token=secrets.token_urlsafe(36),actor_key=secrets.token_urlsafe(36),envelope_key=secrets.token_urlsafe(36))
    restored.validate();app=create_app(restored)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://restore') as client:
        now=int(time.time());headers={'X-PowerSystem-Token':restored.service_token,'X-MA-Actor':sign({'aud':'ma-ops','kid':'ma-v1','iat':now,'exp':now+60,'user_id':999001,'role':'admin'},restored.actor_key)}
        response=await client.get('/internal/ma/v1/tasks/'+str(tid),headers=headers);response.raise_for_status();report=response.json()
        blocked=await client.post('/internal/ma/v1/tasks/'+str(tid)+'/cancel',headers=headers,json={})
        await asyncio.sleep(2)
        checks={'queued_preserved':report['status']=='queued','approved_preserved':report['approvals'][0]['status']=='approved','no_approval_button':report['approvals'][0]['can_decide'] is False,'post_blocked':blocked.status_code==403,'worker_not_started':not app.state.worker.jobs,'no_model_credentials':not restored.llm_key,'no_executor_credentials':not restored.executor_token and not restored.executor_write_key}
    after={r['tablename']:await owner.fetchval('SELECT count(*) FROM '+r['tablename']) for r in tables}
    checks['all_counts_unchanged']=before==after
    checks['no_execution_replay']=after['ma_execution_attempts']==counts['ma_execution_attempts']
    checks['no_security_replay']=after['ma_security_reviews']==counts['ma_security_reviews']
    await owner.close()
    (out/'checks.json').write_text(json.dumps({'checks':checks,'database':name,'initial_counts':counts,'fixture_counts':before,'after_counts':after,'program_asgi_with_real_restored_pg':True},indent=2),encoding='utf-8')
    print(json.dumps(checks))
    if not all(checks.values()):raise ValueError('local_restore_failed')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--output',required=True);asyncio.run(main(p.parse_args()))
