"""Real local Security dependency loss; existing task evidence retained, no fallback."""
import argparse,asyncio,json,time
from pathlib import Path
import asyncpg,httpx
ROOT=Path(__file__).resolve().parents[3]
async def main(a):
    run=(ROOT/a.run).resolve();out=(ROOT/a.output).resolve();out.mkdir(exist_ok=False)
    env=dict(l.split('=',1) for l in (run/'private/local.env').read_text().splitlines() if '=' in l)
    if not out.is_relative_to(run) or env['OPS_BACKEND_URL']!='http://127.0.0.1:18106' or not env['OPS_DATABASE_URL'].endswith(':15435/sec_m4_acceptance_test'):raise ValueError('owned_local_required')
    async with httpx.AsyncClient(trust_env=False,timeout=130) as c:
        try:
            await c.get(env['OPS_SECURITY_URL']+'/health',timeout=2)
        except httpx.HTTPError:pass
        else:raise ValueError('security_must_be_offline_for_this_case')
        r=await c.post(env['OPS_BACKEND_URL']+'/api/v1/auth/login',json={'username':'ma_m3_admin','password':Path(env['MA_M3_ADMIN_PASSWORD_FILE']).read_text().strip()});r.raise_for_status();headers={'Authorization':'Bearer '+r.json()['data']['token']}
        db=await asyncpg.connect(env['OPS_DATABASE_URL']);attempts=await db.fetchval('SELECT count(*) FROM ma_execution_attempts')
        text='请对本机SEC-M4隔离演练容器进行只读安全巡检，列出未检查项目。SEC-M4本机依赖故障验收。'
        r=await c.post(env['OPS_BACKEND_URL']+'/api/v1/agent/chat',headers=headers,json={'message':text})
        row=await db.fetchrow("SELECT task_id,status FROM ma_tasks WHERE user_id=1 AND envelope->>'user_request'=$1 ORDER BY created_at DESC LIMIT 1",text)
        if not row:raise ValueError('fault_task_not_created')
        until=time.monotonic()+60
        while True:
            report_r=await c.get(env['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+str(row['task_id']),headers=headers);report_r.raise_for_status();report=report_r.json()['data']
            if report['status'] in ('succeeded','failed','cancelled','timeout'):break
            if time.monotonic()>until:raise ValueError('fault_wait_timeout')
            await asyncio.sleep(1)
        checks={'failed_closed':report['status']=='failed' and report['error']['code']=='security_unavailable','no_sensitive_collection':not any(x.get('name')=='docker.inspect_container' for x in report['tool_calls']),'no_write':not report['execution_attempts'] and attempts==await db.fetchval('SELECT count(*) FROM ma_execution_attempts'),'catalog_fact_preserved':any(x.get('name')=='assets.list' and x['status']=='ok' for x in report['tool_calls']),'failed_review_preserved':any(x['state'] not in ('pending','completed') for x in report['security_reviews'])}
        await db.close();(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));(out/'checks.json').write_text(json.dumps(checks,indent=2));print(json.dumps({'task_id':str(row['task_id']),'chat_http':r.status_code,'checks':checks}))
        if not all(checks.values()):raise ValueError('fault_case_failed')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--output',required=True);asyncio.run(main(p.parse_args()))
