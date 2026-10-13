"""Read real fixture history and verify idempotency/ownership after a UI decision."""
import argparse
import asyncio
import json
import time
from pathlib import Path
import sys
import httpx

ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT/'stages/09-ops-agent'))
from ops_write_contract import sign


async def main(args):
    cfg=dict(s.split('=',1) for s in Path(args.env).read_text(encoding='utf-8-sig').splitlines() if '=' in s and not s.startswith('#'))
    if cfg['POSTGRES_DB']!='ma_m3_business_test' or args.output.exists():raise ValueError('new_m3_evidence_required')
    args.output.mkdir(parents=True)
    seed=json.loads(args.seed.read_text(encoding='utf-8-sig'));tid=seed['task_id'];checks={}
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=20) as client:
        async def login(role):
            r=await client.post(cfg['OPS_BACKEND_URL']+'/api/v1/auth/login',json={'username':'ma_m3_'+role,'password':Path(cfg['MA_M3_'+role.upper()+'_PASSWORD_FILE']).read_text(encoding='utf-8-sig').strip()});r.raise_for_status();return r.json()['data']['token']
        admin=await login('admin');operator=await login('operator');headers={'Authorization':'Bearer '+admin}
        r=await client.get(cfg['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+tid,headers=headers);r.raise_for_status();report=r.json()['data']
        a=report['approvals'][0];path=cfg['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+tid+'/approvals/'+a['approval_id']+'/decision'
        denied=await client.post(path,json={'decision':'approve'},headers={'Authorization':'Bearer '+operator})
        checks['cross_user_approval_404']=denied.status_code==404
        checks['expected_terminal']=report['status']==args.expected
        checks['no_exported_jwt']=admin not in json.dumps(report) and operator not in json.dumps(report)
        if args.expected=='succeeded':
            repeat=await client.post(path,json={'decision':'approve'},headers=headers)
            opposite=await client.post(path,json={'decision':'reject'},headers=headers)
            checks['same_approve_idempotent']=repeat.status_code==200
            checks['opposite_409']=opposite.status_code==409
            checks['single_real_attempt']=len(report['execution_attempts'])==1 and report['execution_attempts'][0]['status']=='succeeded'
            action=next(e for e in report['operation_evidence'] if e['phase']=='execution')
            before=action['data']['before'];after=action['data']['after']
            checks['actual_started_at_changed']=before['started_at']!=after['started_at']
            checks['same_target_healthy']=before['container_id']==after['container_id'] and after['health']=='healthy'
            step=next(s for s in report['steps'] if s['tool']=='docker.restart_drill_container')
            attempt=report['execution_attempts'][0];now=int(time.time())
            g=dict(aud='ma-drill-executor',kid='ma-write-v1',action='restart',iat=now,exp=now+30,task_id=tid,trace_id=report['trace_id'],
                execution_epoch=step['execution_epoch'],step_no=step['step_no'],call_id=attempt['call_id'],user_id=1,role='admin',args_hash=a['args_hash'],
                deadline=time.time()+30,approval_id=a['approval_id'],**{k:after[k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version')})
            key=Path(cfg['OPS_EXECUTOR_WRITE_KEY_FILE']).read_text(encoding='utf-8-sig').strip()
            replay=await client.post('http://127.0.0.1:8097/internal/drill/v1/restart',json={'grant':sign(g,key)})
            checks['real_broker_replay_409']=replay.status_code==409 and replay.json()['detail']=='dispatch_replayed'
        else:
            checks['zero_real_attempt']=not report['execution_attempts']
            if a['status']=='rejected':
                repeat=await client.post(path,json={'decision':'reject'},headers=headers)
                checks['same_reject_idempotent']=repeat.status_code==200
        history=(await client.get(cfg['OPS_BACKEND_URL']+'/api/v1/agent/sessions/'+seed['session_id'],headers=headers)).json()['data']
        checks['original_answer_kept']=history['turns'][0]['response']['answer_markdown']==seed.get('original_answer','本机隔离演练审批已生成。此轮为离线规划fixture，未调用真实模型；批准前不会执行重启。')
        if seed.get('model_real'):
            health=(await client.get(cfg['OPS_URL']+'/health')).json()
            checks['real_operation_model']=report['model_calls']>0 and not health['model'].startswith('fixture')
            checks['real_supervisor_model']=seed.get('main_model_called') is True
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (args.output/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(checks,ensure_ascii=False))
        if not all(checks.values()):raise SystemExit('program verification incomplete')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--env',required=True);p.add_argument('--seed',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--expected',choices=['succeeded','cancelled'],required=True)
    asyncio.run(main(p.parse_args()))
