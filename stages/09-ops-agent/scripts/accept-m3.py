"""Real local dual-model approval acceptance. No JWT/password in exported evidence."""
import argparse
import asyncio
import json
import time
from pathlib import Path
import httpx
from importlib.machinery import SourceFileLoader


def env(p):return dict(s.split('=',1) for s in Path(p).read_text(encoding='utf-8-sig').splitlines() if '=' in s and not s.startswith('#'))


async def main(args):
    cfg=env(args.env);root=Path(__file__).resolve().parents[3]
    if cfg.get('POSTGRES_DB')!='ma_m3_business_test' or cfg['OPS_BACKEND_URL']!='http://127.0.0.1:18103':raise ValueError('dedicated_m3_acceptance_required')
    if cfg.get('MA_DRILL_ACCEPTANCE_ONLY')!='true' or any(cfg.get(k)!='https://hgapi.dieqiyun.top/v1' for k in ('AGENT_LLM_BASE_URL','OPS_LLM_URL')):
        raise ValueError('authorized_drill_gateway_scope_required')
    if args.output.exists():raise ValueError('new_acceptance_batch_required')
    args.output.mkdir(parents=True)
    manifest=SourceFileLoader('m3_manifest',str(Path(__file__).with_name('accept-m2.py'))).load_module().manifest
    before=manifest(root);records=[];checks={}
    (args.output/'source-manifest.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
    async with httpx.AsyncClient(base_url=cfg['OPS_BACKEND_URL'],trust_env=False,follow_redirects=False,timeout=135) as client:
        async def login(role):
            r=await client.post('/api/v1/auth/login',json={'username':'ma_m3_'+role,'password':Path(cfg['MA_M3_'+role.upper()+'_PASSWORD_FILE']).read_text(encoding='utf-8-sig').strip()})
            r.raise_for_status();return r.json()['data']['token']
        admin=await login('admin');operator=await login('operator');client.headers['Authorization']='Bearer '+admin
        async def state(tid):
            r=await client.get('/api/v1/agent/tasks/'+tid);r.raise_for_status();return r.json()['data']
        async def poll(task,stop):
            until=time.monotonic()+310
            while task['status'] not in stop and time.monotonic()<until:
                await asyncio.sleep(1);task=await state(task['task_id'])
            return task
        try:
            if args.task:
                task=await state(args.task)
            else:
                r=await client.post('/api/v1/agent/chat',json={'message':'请重启本机Docker演练环境中登记的本机隔离演练容器 ma-m3-drill；仅生成管理员审批卡，等待我在页面或审批接口明确批准。范围限定为本次演练对象。'})
                r.raise_for_status();chat=r.json()['data'];records.append({'chat':chat})
                if not chat.get('task_cards'):raise AssertionError('real_supervisor_did_not_delegate')
                task=await poll(chat['task_cards'][0],{'awaiting_approval','succeeded','failed','cancelled','timeout'})
                checks['real_supervisor_delegated']=bool(chat['task_cards'])
                checks['real_operation_model']=task['model_calls']>0
                history=(await client.get('/api/v1/agent/sessions/'+chat['session_id'])).json()['data']
                checks['original_answer_bound']=task['parent_turn_id']==chat['turn_id'] and history['turns'][0]['response']['answer_markdown']==chat['answer_markdown']
            records.append({'before_task':task})
            checks['pending_approval']=task['status']=='awaiting_approval' and bool(task['approvals'])
            checks['zero_write_before_approval']=not task['execution_attempts']
            if not checks['pending_approval']:raise AssertionError('real_operation_did_not_prepare_approval')
            a=task['approvals'][0];tid=task['task_id'];path='/api/v1/agent/tasks/'+tid+'/approvals/'+a['approval_id']+'/decision'
            cross=await client.get('/api/v1/agent/tasks/'+tid,headers={'Authorization':'Bearer '+operator})
            negative=await client.post(path,json={'decision':'approve'},headers={'Authorization':'Bearer '+operator})
            checks['cross_user_read_404']=cross.status_code==404
            checks['cross_user_approval_404']=negative.status_code==404
            bad=await client.post(path,json={'decision':'approve','role':'admin'})
            checks['extra_decision_fields_400']=bad.status_code==400
            if args.mode=='prepare':
                checks['preflight_stateless']=a['plan']['snapshot']['stateless_verified']
            elif args.mode=='cancel':
                r=await client.post('/api/v1/agent/tasks/'+tid+'/cancel');r.raise_for_status();task=r.json()['data']
                checks['cancel_zero_write']=task['status']=='cancelled' and not task['execution_attempts']
            else:
                decision='approve' if args.mode=='execute' else 'reject'
                r=await client.post(path,json={'decision':decision});r.raise_for_status()
                repeat=await client.post(path,json={'decision':decision});repeat.raise_for_status()
                opposite=await client.post(path,json={'decision':'reject' if decision=='approve' else 'approve'})
                checks['same_decision_idempotent']=repeat.status_code==200
                checks['opposite_decision_409']=opposite.status_code==409
                task=await poll(r.json()['data'],{'succeeded','failed','cancelled','timeout'})
                if args.mode=='execute':
                    checks['real_restart_succeeded']=task['status']=='succeeded'
                    checks['single_attempt']=len(task['execution_attempts'])==1 and task['execution_attempts'][0]['status']=='succeeded'
                    checks['approval_consumed']=bool(task['approvals'][0]['consumed_at'])
                    evidence=[e for e in task['operation_evidence'] if e['phase']=='execution']
                    checks['before_after_health']=bool(evidence) and evidence[0]['data']['before']['started_at']!=evidence[0]['data']['after']['started_at'] and evidence[0]['data']['after']['health']=='healthy'
                else:checks['reject_zero_write']=task['status']=='cancelled' and not task['execution_attempts']
            records.append({'final_task':task})
            checks['no_credentials_exported']=not any(v in json.dumps(records,ensure_ascii=False) for v in (admin,operator,Path(cfg['OPS_EXECUTOR_WRITE_KEY_FILE']).read_text(encoding='utf-8-sig').strip()))
            print('MA-M3',args.mode,task['task_id'],task['status'],flush=True)
        finally:
            checks['source_unchanged']=manifest(root)==before
            (args.output/'records.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
            (args.output/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(checks,ensure_ascii=False),flush=True)
    if not checks or not all(checks.values()):raise SystemExit('MA-M3 acceptance incomplete; preserve this batch.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--env',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--mode',choices=['prepare','execute','reject','cancel'],default='prepare');p.add_argument('--task')
    asyncio.run(main(p.parse_args()))
