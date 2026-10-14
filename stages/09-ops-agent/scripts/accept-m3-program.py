"""Fixture planner + real Go identity, PostgreSQL, adapter, Docker and saved UI history."""
import argparse
import asyncio
import json
import os
import time
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4,UUID
import sys
import httpx

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'stages/09-ops-agent'),str(ROOT/'stages/08-agent-rag')]
from ops_agent.security import claims,sign,digest
from rag_agent.ops import envelope_signature
from rag_agent.store import Store as ChatStore


async def main(args):
    cfg=dict(s.split('=',1) for s in Path(args.env).read_text(encoding='utf-8-sig').splitlines() if '=' in s and not s.startswith('#'))
    if cfg['POSTGRES_DB']!='ma_m3_business_test':raise ValueError('m3_fixture_required')
    if args.output.exists():raise ValueError('new_program_evidence_required')
    args.output.mkdir(parents=True);records=[];checks={}
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=20) as client:
        health=(await client.get(cfg['OPS_URL']+'/health')).json()
        if health['model']!='fixture-m3-offline-planner':raise ValueError('explicit_offline_planner_required')
        login=await client.post(cfg['OPS_BACKEND_URL']+'/api/v1/auth/login',json={'username':'ma_m3_admin','password':Path(cfg['MA_M3_ADMIN_PASSWORD_FILE']).read_text(encoding='utf-8-sig').strip()});login.raise_for_status()
        data=login.json()['data'];jwt=data['token'];actor={'user_id':data['user']['id'],'role':data['user']['role']}
        task_id=uuid4();sid=uuid4();trace=uuid4();question='请重启本机隔离演练容器 ma-m3-drill，等待程序审批。'
        task=dict(protocol='ma/v1',task_id=str(task_id),trace_id=str(trace),idempotency_key=str(uuid4()),session_id=str(sid),parent_turn_id=None,
            actor=actor,intent='离线规划fixture · 本机演练重启审批',user_request=question,targets=[{'description':'本机隔离演练容器 ma-m3-drill','asset_id':None}],
            operation='restart_drill_container',constraints={'max_risk_class':'R2','timeout_sec':300},created_at=datetime.now(timezone.utc).isoformat())
        headers={'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':Path(cfg['OPS_SERVICE_TOKEN_FILE']).read_text(encoding='utf-8-sig').strip(),
            'X-MA-Actor':sign(claims(actor,sid,trace,__import__('hashlib').sha256(question.encode()).hexdigest()),Path(cfg['MA_ACTOR_KEY_FILE']).read_text(encoding='utf-8-sig').strip()),
            'X-MA-Envelope':envelope_signature(task,Path(cfg['MA_ENVELOPE_KEY_FILE']).read_text(encoding='utf-8-sig').strip())}
        response=await client.post(cfg['OPS_URL']+'/internal/ma/v1/tasks',json=task,headers=headers);response.raise_for_status()
        public={'Authorization':'Bearer '+jwt};until=time.monotonic()+30
        while True:
            report=(await client.get(cfg['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+str(task_id),headers=public)).json()['data']
            if report['status'] in ('awaiting_approval','failed','succeeded','cancelled','timeout'):break
            if time.monotonic()>until:raise TimeoutError('fixture planning timeout')
            await asyncio.sleep(.5)
        checks['offline_planner_explicit']=health['model']=='fixture-m3-offline-planner'
        checks['real_pending_approval']=report['status']=='awaiting_approval'
        checks['zero_write_before_approval']=not report['execution_attempts']
        checks['real_r0_inspect']=any(c['data'] and c['data'].get('image_digest') for c in report['calls'])
        if not checks['real_pending_approval']:
            (args.output/'failed-task.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            raise ValueError('program_preflight_failed')
        store=await ChatStore.open(cfg['RAG_DATABASE_URL'])
        try:
            await store.ensure(sid,actor['user_id'],'MA-M3程序演练：'+args.label,create=True)
            answer='本机隔离演练审批已生成。此轮为离线规划fixture，未调用真实模型；批准前不会执行重启。'
            saved={'request_id':str(uuid4()),'session_id':str(sid),'status':'answered','answer_markdown':answer,'sources':[],'tool_calls':[],
                'warnings':['离线规划fixture，不代表真实双模型验收。'],'model':'fixture-m3-offline-planner','model_called':False,'task_cards':[report],'response_mode':'task_dispatched'}
            turn=await store.save(sid,question,saved,[])
            bind={'session_id':str(sid),'parent_turn_id':turn}
            headers['X-MA-Envelope']=envelope_signature({'task_id':str(task_id),**bind},Path(cfg['MA_ENVELOPE_KEY_FILE']).read_text(encoding='utf-8-sig').strip())
            r=await client.post(cfg['OPS_URL']+'/internal/ma/v1/tasks/'+str(task_id)+'/bind-turn',json=bind,headers=headers);r.raise_for_status()
        finally:await store.pool.close()
        records={'task_id':str(task_id),'session_id':str(sid),'approval_id':report['approvals'][0]['approval_id'],'report':report,'model_real':False,'label':args.label}
        (args.output/'records.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
        (args.output/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'task_id':str(task_id),'session_id':str(sid),'label':args.label,'checks':checks},ensure_ascii=False))
        if not all(checks.values()):raise SystemExit('program acceptance incomplete')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--env',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--label',default='审批与真实重启')
    asyncio.run(main(p.parse_args()))
