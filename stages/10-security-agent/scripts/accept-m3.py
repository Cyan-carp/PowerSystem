"""Actual local SEC-M3 chain. Never approves a write; the owner uses the task card."""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[3]


def read_env(path):
    return dict(line.split('=',1) for line in path.read_text(encoding='utf-8-sig').splitlines() if '=' in line and not line.startswith('#'))


def manifest():
    paths=[]
    for folder in ('stages/09-ops-agent','stages/10-security-agent','stages/08-agent-rag/rag_agent','stages/04-frontend/src'):
        paths.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc',) and p.name!='.env')
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


async def main(args):
    run=(ROOT/args.run).resolve();out=(ROOT/args.output).resolve()
    if not run.is_relative_to(ROOT/'artifacts/stage10-security-agent') or not out.is_relative_to(run): raise ValueError('isolated_output_required')
    out.mkdir(exist_ok=False);before=manifest();cfg=read_env(run/'private/local.env')
    if cfg['POSTGRES_PORT']!='15434' or cfg['OPS_BACKEND_URL']!='http://127.0.0.1:18105': raise ValueError('dedicated_sec_m3_chain_required')
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=130) as client:
        checks={};ready_until=time.monotonic()+60
        endpoints=[('proxy',cfg['OPS_BACKEND_URL']+'/api/v1/ping'),('security',cfg['OPS_SECURITY_URL']+'/health'),('ops',cfg['OPS_URL']+'/health'),('adapter',cfg['OPS_EXECUTOR_URL']+'/health'),('supervisor',cfg['OPS_SUPERVISOR_URL']+'/health')]
        while True:
            for name,url in endpoints:
                try: checks[name]=(await client.get(url,timeout=2)).status_code==200
                except httpx.HTTPError: checks[name]=False
            if all(checks.values()) or time.monotonic()>=ready_until:break
            await asyncio.sleep(1)
        (out/'health.json').write_text(json.dumps(checks),encoding='utf-8')
        if not all(checks.values()): raise ValueError('local_chain_not_ready')
        password=Path(cfg['MA_M3_ADMIN_PASSWORD_FILE']).read_text().strip()
        login=await client.post(cfg['OPS_BACKEND_URL']+'/api/v1/auth/login',json={'username':'ma_m3_admin','password':password});login.raise_for_status()
        jwt=login.json()['data']['token'];headers={'Authorization':'Bearer '+jwt}
        private_headers={**headers,'X-PowerSystem-Token':Path(cfg['OPS_SERVICE_TOKEN_FILE']).read_text().strip()}
        actor=await client.get(cfg['OPS_BACKEND_URL']+'/internal/ma/v1/actor',headers=private_headers);actor.raise_for_status()
        private_headers['X-MA-Actor']=actor.json()['data']['context']
        if args.phase=='import':
            registry=await client.get(cfg['OPS_EXECUTOR_URL']+'/internal/ma-exec/v1/registry',headers={'X-MA-Executor-Token':Path(cfg['OPS_EXECUTOR_TOKEN_FILE']).read_text().strip()});registry.raise_for_status()
            response=await client.post(cfg['OPS_URL']+'/internal/ma/v1/assets/import',headers=private_headers,json={'registry_hash':registry.json()['registry_hash']});response.raise_for_status()
            value=response.json()
        elif args.phase in ('read','target','explain'):
            question={'read':'请对本项目进行安全巡检，只做登记资产的只读检查，列出未检查项目。', 'target':'请对本机SEC-M3隔离演练容器进行安全巡检，只读检查并列出未检查项目。','write':'请重启本机SEC-M3隔离演练容器。','explain':'请解释本人任务 '+str(args.task)+' 的结果。'}[args.phase]
            started=time.monotonic();response=await client.post(cfg['OPS_BACKEND_URL']+'/api/v1/agent/chat',headers=headers,json={'message':question});response.raise_for_status()
            value=response.json()['data']
            cards=value.get('task_cards',[])
            if not cards: raise ValueError('no_task_card_returned')
            tid=cards[-1]['task_id'];until=time.monotonic()+300
            while True:
                r=await client.get(cfg['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+tid,headers=headers);r.raise_for_status();report=r.json()['data']
                if report['status'] in ('succeeded','failed','cancelled','timeout','awaiting_approval'):break
                if time.monotonic()>until:raise ValueError('acceptance_wait_timeout')
                await asyncio.sleep(2)
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            (out/'chat.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
            value={'task_id':tid,'session_id':value['session_id'],'task_status':report['status'],
                'duration_seconds':round(time.monotonic()-started,3),'model_calls':report['model_calls'],
                'reviews':[{'kind':x['kind'],'state':x['state'],'decision':x['decision']} for x in report['security_reviews']],
                'write_attempts':len(report['execution_attempts'])}
        elif args.phase=='report':
            r=await client.get(cfg['OPS_BACKEND_URL']+'/api/v1/agent/tasks/'+args.task,headers=headers);r.raise_for_status()
            (out/'report.json').write_text(json.dumps(r.json()['data'],ensure_ascii=False,indent=2),encoding='utf-8')
            value={'task_id':args.task,'status':r.json()['data']['status']}
        if args.phase in ('read','target'):
            value['inspection_report_present']=report.get('task_kind')=='security_inspection' and bool(report.get('inspection_report'))
            value['zero_write']=not report['execution_attempts'] and not report['approvals']
            value['checks']={i['check_id']:i['status'] for i in (report.get('inspection_report') or {}).get('items',[])}
        after=manifest()
        passed=before==after and (args.phase in ('import','report') or value.get('task_status')==('awaiting_approval' if args.phase=='write' else 'succeeded'))
        if args.phase in ('read','target'): passed=passed and value['inspection_report_present'] and value['zero_write'] and any(i['status']=='not_checked' for i in report['inspection_report']['items']) and any(r['kind']=='inspection_report' and r['decision']=='allow' for r in report['security_reviews']) and not value.get('warnings')
        if args.phase in ('read','target') and any(r['decision']!='allow' for r in report['security_reviews']):passed=False
        if args.phase=='explain':
            passed=passed and bool(report['security_reviews']) and report['security_reviews'][-1]['kind']=='data_egress' and report['security_reviews'][-1]['decision']=='allow' and report['security_reviews'][-1]['state']=='completed'
        summary={'phase':args.phase,'source_unchanged':before==after,'passed':passed,'result':value}
        (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'source-manifest.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(summary,ensure_ascii=False))
        if not passed: raise ValueError('acceptance_condition_failed')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',required=True);p.add_argument('--output',required=True);p.add_argument('--phase',choices=('import','read','target','explain','report'),required=True);p.add_argument('--task')
    asyncio.run(main(p.parse_args()))
