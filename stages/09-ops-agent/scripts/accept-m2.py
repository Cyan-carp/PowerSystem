"""Real Supervisor -> Operation -> adapter -> R730XD acceptance, with safe record export."""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
import httpx


def env(path):return dict(line.split('=',1) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if '=' in line and not line.startswith('#'))


def manifest(root):
    files=[]
    for folder in ('stages/09-ops-agent','stages/08-agent-rag/rag_agent','stages/02-backend/cmd/ma-local-api','stages/02-backend/internal','stages/04-frontend/src'):
        for p in sorted((root/folder).rglob('*')):
            if p.is_file() and p.suffix in ('.py','.sql','.go','.vue','.ts'):
                files.append({'path':p.relative_to(root).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    return files


async def main(args):
    cfg=env(args.env);root=Path(__file__).resolve().parents[3]
    if args.output.exists():raise ValueError('new_acceptance_batch_required')
    args.output.mkdir(parents=True)
    before=manifest(root);(args.output/'source-manifest.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
    records=[];checks={};password=Path(cfg['MA_M2_OPERATOR_PASSWORD_FILE']).read_text().strip()
    async with httpx.AsyncClient(base_url='http://127.0.0.1:18082',trust_env=False,follow_redirects=False,timeout=135) as client:
        login=await client.post('/api/v1/auth/login',json={'username':'ma_m2_operator','password':password});login.raise_for_status()
        client.headers['Authorization']='Bearer '+login.json()['data']['token']
        scenarios=[
            ('host','请查询R730XD项目运行主机现在的CPU、内存和磁盘使用情况，给出实际采集时间并区分程序事实和原因假设。',{'linux.get_cpu_usage','linux.get_memory_usage','linux.get_disk_status'}),
            ('container','请查询本项目API容器现在的运行状态、CPU和内存，以及最近100行日志的脱敏事件摘要。说明过滤和截断，不读取其他项目。',{'docker.inspect_container','docker.get_container_stats','docker.get_container_logs'}),
            ('database','请查询R730XD平台PostgreSQL数据库现在的健康和连接聚合统计，不读取SQL正文或业务记录。',{'postgresql.get_db_status','postgresql.get_connection_stats'}),
        ]
        if args.only:scenarios=[s for s in scenarios if s[0]==args.only]
        try:
            for label,question,required in scenarios:
                started=time.monotonic()
                response=await client.post('/api/v1/agent/chat',json={'message':question});response.raise_for_status();chat=response.json()['data']
                records.append({'scenario':label,'chat':chat})
                cards=chat.get('task_cards',[]);checks[label+'_delegated']=bool(cards)
                if not cards:
                    print(label,'not_delegated',flush=True);continue
                task=cards[0];until=time.monotonic()+310
                while task['status'] not in ('succeeded','failed','cancelled','timeout') and time.monotonic()<until:
                    await asyncio.sleep(2)
                    r=await client.get('/api/v1/agent/tasks/'+task['task_id']);r.raise_for_status();task=r.json()['data']
                r=await client.get('/api/v1/agent/tasks/'+task['task_id']);r.raise_for_status();task=r.json()['data']
                records.append({'scenario':label,'final_task':task,'seconds':round(time.monotonic()-started,3)})
                actual={c['name'] for c in task.get('tool_calls',[]) if c['status']=='ok'}
                checks[label+'_succeeded']=task['status']=='succeeded'
                checks[label+'_real_required_tools']=required<=actual
                checks[label+'_separate_evidence']=task['evidence']['state']=='not_queried' and bool(task.get('infrastructure_evidence'))
                infrastructure_steps=[s for s in task['steps'] if s['tool'].startswith(('linux.','docker.','postgresql.'))]
                checks[label+'_asset_versions']=bool(infrastructure_steps) and all(s.get('asset_id') and s.get('asset_version') for s in infrastructure_steps)
                checks[label+'_no_private_results']=not any(s in json.dumps(task,ensure_ascii=False) for s in ('password_file','private_key_file','NetworkSettings','SSH_ORIGINAL_COMMAND','X-MA-Actor'))
                history=(await client.get('/api/v1/agent/sessions/'+chat['session_id'])).json()['data']
                checks[label+'_bound_history']=task['parent_turn_id']==chat['turn_id'] and history['turns'][0]['response']['answer_markdown']==chat['answer_markdown']
                if label=='container':
                    logs=[c['data'] for c in task['calls'] if c['data'] and 'events' in c['data']]
                    checks['log_filter_is_explicit']=bool(logs) and all(d['complete'] is False and 'filtered_lines' in d for d in logs)
                print(label,task['status'],sorted(actual),flush=True)
            # Real identity/owner negative checks use the independent admin fixture, never impersonate a user.
            admin_password=Path(cfg['MA_M2_ADMIN_PASSWORD_FILE']).read_text().strip()
            admin=await client.post('/api/v1/auth/login',json={'username':'ma_m2_admin','password':admin_password});admin.raise_for_status()
            tasks=[r['final_task'] for r in records if 'final_task' in r]
            if tasks:
                denied=await client.get('/api/v1/agent/tasks/'+tasks[0]['task_id'],headers={'Authorization':'Bearer '+admin.json()['data']['token']})
                checks['admin_cross_user_404']=denied.status_code==404
            checks['execution_disabled']=not (await client.get('http://127.0.0.1:8096/health')).json()['write_tools_registered']
        finally:
            checks['source_unchanged']=manifest(root)==before
            (args.output/'records.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
            (args.output/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(checks,ensure_ascii=False),flush=True)
    if not checks or not all(checks.values()):raise SystemExit('MA-M2 real acceptance incomplete; retain this batch.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--env',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--only',choices=['host','container','database'])
    asyncio.run(main(p.parse_args()))
