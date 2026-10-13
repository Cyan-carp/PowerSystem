"""Current-role negative probe on the explicitly disposable MA-M3 admin fixture only."""
import argparse
import asyncio
import json
from pathlib import Path
import asyncpg
import httpx


async def main(args):
    cfg=dict(s.split('=',1) for s in Path(args.env).read_text(encoding='utf-8-sig').splitlines() if '=' in s and not s.startswith('#'))
    if cfg['POSTGRES_DB']!='ma_m3_business_test' or cfg['POSTGRES_HOST']!='127.0.0.1' or args.output.exists():raise ValueError('owned_m3_fixture_required')
    args.output.mkdir(parents=True);seed=json.loads(args.seed.read_text(encoding='utf-8-sig'));tid=seed['task_id'];checks={}
    db=await asyncpg.connect(host='127.0.0.1',port=5432,user=cfg['POSTGRES_USER'],password=cfg['POSTGRES_PASSWORD'],database='ma_m3_business_test')
    changed=False
    try:
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=20) as c:
            r=await c.post(cfg['OPS_BACKEND_URL']+'/api/v1/auth/login',json={'username':'ma_m3_admin','password':Path(cfg['MA_M3_ADMIN_PASSWORD_FILE']).read_text(encoding='utf-8-sig').strip()});r.raise_for_status()
            headers={'Authorization':'Bearer '+r.json()['data']['token']}
            if await db.fetchval("SELECT role FROM users WHERE username='ma_m3_admin'")!='admin':raise ValueError('expected_admin_fixture')
            await db.execute("UPDATE users SET role='operator' WHERE username='ma_m3_admin' AND role='admin'");changed=True
            r=await c.get(cfg['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+tid,headers=headers);r.raise_for_status();task=r.json()['data']
            a=task['approvals'][0]
            decision=await c.post(cfg['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+tid+'/approvals/'+a['approval_id']+'/decision',headers=headers,json={'decision':'approve'})
            checks={'old_admin_jwt_current_operator_403':decision.status_code==403,'server_can_decide_false':a.get('can_decide') is False,
                    'approval_still_pending':a['status']=='pending','zero_execution_attempts':not task['execution_attempts']}
            (args.output/'checks.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
    finally:
        if changed:await db.execute("UPDATE users SET role='admin' WHERE username='ma_m3_admin' AND role='operator'")
        await db.close()
    print(json.dumps(checks))
    if not all(checks.values()):raise SystemExit('current role probe incomplete')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--env',required=True);p.add_argument('--seed',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    asyncio.run(main(p.parse_args()))
