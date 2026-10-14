"""Actual fixed-flow verification; synthetic local business data only, zero models."""
import argparse,asyncio,json,time
from pathlib import Path
from uuid import uuid4
import httpx,asyncpg
from uuid import UUID
ROOT=Path(__file__).resolve().parents[3]
async def main(a):
    run=(ROOT/a.run).resolve();out=(ROOT/a.output).resolve();out.mkdir(exist_ok=False)
    env=dict(l.split('=',1) for l in (run/'private/local.env').read_text().splitlines() if '=' in l)
    if not run.name.startswith('sec-m4-') or not out.is_relative_to(run) or env['OPS_BACKEND_URL']!='http://127.0.0.1:18106':raise ValueError('dedicated_local_required')
    baseline=json.loads((run/'real-platform-v2-r2/summary.json').read_text())['result'];sid=baseline['session_id']
    async with httpx.AsyncClient(trust_env=False,timeout=130) as c:
        r=await c.post(env['OPS_BACKEND_URL']+'/api/v1/auth/login',json={'username':'ma_m3_admin','password':Path(env['MA_M3_ADMIN_PASSWORD_FILE']).read_text().strip()});r.raise_for_status();headers={'Authorization':'Bearer '+r.json()['data']['token']}
        r=await c.get(env['OPS_BACKEND_URL']+'/api/v1/agent/sessions/'+sid,headers=headers);r.raise_for_status();turn=r.json()['data']['turns'][0]
        body={'parent_turn_id':turn['id'],'device_id':2,'checks':['latest_telemetry','current_alarms','prediction'],'verification_id':str(uuid4())}
        url=env['OPS_BACKEND_URL']+'/api/v1/agent/sessions/'+sid+'/verify'
        first=await c.post(url,headers=headers,json=body);first.raise_for_status();response=first.json()['data']
        db=await asyncpg.connect(env['OPS_DATABASE_URL'])
        tid=await db.fetchval('SELECT task_id FROM ma_tasks WHERE idempotency_key=$1 AND session_id=$2 AND user_id=1',UUID(body['verification_id']),UUID(sid))
        if not tid:raise ValueError('fixed_task_not_found')
        r=await c.get(env['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+str(tid),headers=headers);r.raise_for_status();report=r.json()['data']
        until=time.monotonic()+120
        while report['status'] not in ('succeeded','failed','cancelled','timeout'):
            if time.monotonic()>until:raise ValueError('fixed_timeout')
            await asyncio.sleep(1);r=await c.get(env['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+report['task_id'],headers=headers);r.raise_for_status();report=r.json()['data']
        replay=await c.post(url,headers=headers,json=body);replay.raise_for_status();replayed=replay.json()['data']
        checks={'succeeded':report['status']=='succeeded','supervisor_zero_model':response.get('kind')=='verification' and response.get('model_called',False) is False and response.get('model','').startswith('程序只读核验'),'operation_zero_model':report['model_calls']==0,'security_zero_model':not report['security_reviews'],'zero_write':not report['execution_attempts'],'three_checks_complete':len(report['verification_results'])==3 and all(x['status']=='ok' for x in report['verification_results'].values()),'same_turn_replayed':response['turn_id']==replayed['turn_id'],'same_task_replayed':await db.fetchval('SELECT count(*) FROM ma_tasks WHERE idempotency_key=$1 AND session_id=$2',UUID(body['verification_id']),UUID(sid))==1}
        await db.close()
        (out/'response.json').write_text(json.dumps(response,ensure_ascii=False,indent=2));(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));(out/'checks.json').write_text(json.dumps(checks,indent=2));print(json.dumps(checks))
        if not all(checks.values()):raise ValueError('fixed_verification_failed')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--output',required=True);asyncio.run(main(p.parse_args()))
