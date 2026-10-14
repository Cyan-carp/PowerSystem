"""Local closure, ownership and actual restart recovery. Never approves an action."""
import argparse
import asyncio
import json
import sys
import time
import subprocess
from datetime import datetime,timezone
from importlib import import_module
from pathlib import Path
from uuid import uuid4,UUID
import asyncpg
import httpx

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'stages/09-ops-agent'),str(ROOT/'stages/10-security-agent')]
from ops_write_contract import sign
from review_contract import digest
helpers=import_module('accept-m2')

async def history(db):
    rows=await db.fetch('SELECT review_id,state,request_hash,decision_hash,signature_validated FROM ma_security_reviews ORDER BY review_id')
    tasks=await db.fetch('SELECT task_id,status,execution_epoch,model_calls FROM ma_tasks ORDER BY task_id')
    events=await db.fetch('SELECT * FROM ma_security_review_events ORDER BY event_seq')
    return json.loads(json.dumps({'reviews':[dict(r) for r in rows],'tasks':[dict(r) for r in tasks],
        'events_count':len(events),'events_hash':digest(json.loads(json.dumps([dict(r) for r in events],default=str)))},default=str))

def drill_state(run):
    owned=json.loads((run/'drill.json').read_text())
    reply=subprocess.run(['docker','inspect',owned['container_id']],capture_output=True,text=True,encoding='utf-8')
    if reply.returncode:raise ValueError('owned_drill_unavailable')
    value=json.loads(reply.stdout)[0]
    if value['Config']['Labels']['com.docker.compose.project']!=owned['project']:raise ValueError('drill_not_owned')
    return {'container_id':value['Id'],'started_at':value['State']['StartedAt'],'health':value['State'].get('Health',{}).get('Status')}

async def main(args):
    run=(ROOT/args.run).resolve();out=(ROOT/args.output).resolve()
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent') or not out.is_relative_to(run):raise ValueError('isolated_output_required')
    cfg=helpers.read_env(run/'private/local.env')
    if cfg['POSTGRES_PORT']!='15433' or not cfg['OPS_DATABASE_URL'].endswith('/sec_m2_acceptance_test'):raise ValueError('isolated_m2_database_required')
    db=await asyncpg.connect(cfg['OPS_DATABASE_URL'])
    try:
        if args.phase=='prepare-recovery':
            out.mkdir(exist_ok=False)
            if await db.fetchval("SELECT count(*) FROM ma_tasks WHERE status IN ('planning','executing','queued','awaiting_approval')"):raise ValueError('active_tasks_block_recovery')
            old=await db.fetchrow("SELECT r.*,g.material,g.material_hash,g.role FROM ma_security_reviews r JOIN ma_security_gate_contexts g USING(review_id) WHERE r.kind='data_egress' AND r.state='completed' ORDER BY r.created_at DESC LIMIT 1")
            rid=uuid4();request=json.loads(old['request']);request['review_id']=str(rid)
            async with db.transaction():
                await db.execute('INSERT INTO ma_security_gate_contexts(review_id,task_id,step_no,execution_epoch,user_id,role,kind,material,material_hash) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)',rid,old['task_id'],old['step_no'],old['execution_epoch'],old['user_id'],old['role'],old['kind'],old['material'],old['material_hash'])
                await db.execute("INSERT INTO ma_security_reviews(review_id,task_id,step_no,trace_id,user_id,execution_epoch,kind,request_hash,binding_hash,request,state,model) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$8,$9::jsonb,'pending',$10)",rid,old['task_id'],old['step_no'],old['trace_id'],old['user_id'],old['execution_epoch'],old['kind'],digest(request),json.dumps(request),old['model'])
                await db.execute("INSERT INTO ma_security_review_events(review_id,state,reason) VALUES($1,'pending','controlled_recovery_fixture')",rid)
            (out/'before.json').write_text(json.dumps({'snapshot':await history(db),'pending':str(rid),'drill':drill_state(run)},ensure_ascii=False,indent=2),encoding='utf-8')
            summary={'prepared_pending':1,'model_calls':0,'execution_authorized':False}
        elif args.phase=='verify-recovery':
            before=json.loads((out/'before.json').read_text());after=await history(db)
            pending=await db.fetchrow('SELECT * FROM ma_security_reviews WHERE review_id=$1',UUID(before['pending']))
            previous=[r for r in before['snapshot']['reviews'] if r['review_id']!=before['pending']]
            retained=[r for r in after['reviews'] if r['review_id']!=before['pending']]
            passed=previous==retained and before['snapshot']['tasks']==after['tasks'] and pending['state']=='unavailable' and pending['error_code']=='review_interrupted' and not pending['signature_validated'] and pending['decision'] is None and after['events_count']==before['snapshot']['events_count']+1 and drill_state(run)==before['drill']
            summary={'passed':passed,'historical_reviews_retained':len(previous),'pending_recovered':1,'task_model_counts_unchanged':before['snapshot']['tasks']==after['tasks'],'execution_authorized':False,'drill_unchanged':drill_state(run)==before['drill']}
        elif args.phase=='closed':
            out.mkdir(exist_ok=False);window=json.loads((run/'private/broker/write-window.json').read_text())
            if window['write_enabled'] or cfg['OPS_WRITE_ENABLED']!='false':raise ValueError('close_window_first')
            a=await db.fetchrow("SELECT a.*,t.trace_id,t.user_id,t.execution_epoch FROM ma_approvals a JOIN ma_tasks t USING(task_id) WHERE a.consumed_at IS NOT NULL ORDER BY a.decided_at DESC LIMIT 1")
            if not a:raise ValueError('approved_execution_evidence_required')
            plan=json.loads(a['plan']);now=int(time.time())
            grant={'aud':'ma-drill-executor','kid':'ma-write-v1','action':'restart','iat':now,'exp':now+30,
                'task_id':str(a['task_id']),'trace_id':str(a['trace_id']),'execution_epoch':a['execution_epoch'],'step_no':a['step_no'],
                'call_id':str(uuid4()),'user_id':a['user_id'],'role':'admin','args_hash':a['args_hash'],'deadline':time.time()+30,'approval_id':str(a['approval_id']),
                **{k:plan['snapshot'][k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version')}}
            before=drill_state(run);attempts=await db.fetchval('SELECT count(*) FROM ma_execution_attempts')
            key=Path(cfg['OPS_EXECUTOR_WRITE_KEY_FILE']).read_text().strip()
            async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=10) as client:
                response=await client.post('http://127.0.0.1:8103/internal/drill/v1/restart',json={'grant':sign(grant,key)})
                login=await client.post(cfg['OPS_BACKEND_URL']+'/api/v1/auth/login',json={'username':'ma_m3_operator','password':Path(cfg['MA_M3_OPERATOR_PASSWORD_FILE']).read_text().strip()});login.raise_for_status()
                other=await client.get(cfg['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+str(a['task_id']),headers={'Authorization':'Bearer '+login.json()['data']['token']})
            error=response.json().get('detail')
            summary={'passed':response.status_code==403 and error=='write_disabled' and drill_state(run)==before and other.status_code==404 and await db.fetchval('SELECT count(*) FROM ma_execution_attempts')==attempts,
                'broker_status':response.status_code,'broker_error':error,'drill_unchanged':drill_state(run)==before,'cross_user_status':other.status_code,'attempts_unchanged':await db.fetchval('SELECT count(*) FROM ma_execution_attempts')==attempts,'model_calls':0,'execution_authorized':False}
        if args.phase!='prepare-recovery':
            p=out/'summary.json'
            if p.exists():raise ValueError('independent_attempt_required')
            p.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(summary))
        if summary.get('passed') is False:raise ValueError('verification_failed')
    finally:await db.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--output',required=True)
    p.add_argument('--phase',choices=('prepare-recovery','verify-recovery','closed'),required=True)
    asyncio.run(main(p.parse_args()))
