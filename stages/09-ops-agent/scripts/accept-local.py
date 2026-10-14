"""Real localhost acceptance. No credentials, bearer tokens or signatures are exported."""
import asyncio
import argparse
import json
import hashlib
import os
import time
from pathlib import Path
from uuid import uuid4
import httpx


async def run(args):
    assert args.url == 'http://127.0.0.1:18081'
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[3]
    source_files = []
    for folder in ('stages/02-backend/internal', 'stages/02-backend/cmd/ma-local-api', 'stages/04-frontend/src', 'stages/08-agent-rag/rag_agent', 'stages/09-ops-agent/ops_agent', 'stages/09-ops-agent/deploy'):
        for source in sorted((root/folder).rglob('*')):
            if source.is_file() and source.suffix in ('.go', '.py', '.ts', '.vue', '.sql'):
                source_files.append({'path': source.relative_to(root).as_posix(), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
    (args.output/'source-manifest.json').write_text(json.dumps({'capture': 'before_run', 'files': source_files}, ensure_ascii=False, indent=2), encoding='utf-8')
    records=[]; checks={}
    async with httpx.AsyncClient(base_url=args.url, trust_env=False, timeout=135) as client:
        for _ in range(60):
            try:
                ready=await client.get('http://127.0.0.1:18093/health')
                if ready.status_code==200: break
            except httpx.HTTPError: pass
            await asyncio.sleep(1)
        else: raise SystemExit('Supervisor not ready')
        login=await client.post('/api/v1/auth/login',json={'username':os.getenv('NATURAL_TEST_USERNAME','natural_test'),'password':os.environ['NATURAL_TEST_PASSWORD']})
        login.raise_for_status(); client.headers['Authorization']='Bearer '+login.json()['data']['token']
        async def request(method,path,**kwargs):
            for _ in range(3):
                response=await client.request(method,path,**kwargs)
                if response.status_code!=429: return response
                await asyncio.sleep(min(60,int(response.headers.get('Retry-After','2'))))
            return response
        async def chat(text,sid=None):
            started=time.monotonic()
            response=await request('POST','/api/v1/agent/chat',json={'message':text,**({'session_id':sid} if sid else {})})
            response.raise_for_status();data=response.json()['data']
            records.append({'question':text,'seconds':round(time.monotonic()-started,2),'response':data})
            print('chat',text,data['status'],data.get('response_mode'),flush=True)
            return data
        try:
            inventory=(await request('GET','/api/v1/devices',params={'page':1,'page_size':2})).json()['data']['list']
            assert len(inventory)>=2 and inventory[0]['id']<inventory[1]['id'];second=inventory[1]['id']
            daily=await chat('你好'); mixed=await chat('解释TCP，顺便推荐晚饭')
            checks['daily_zero_model_tool_task']=all(not x.get('model_called') and not x.get('tool_calls') and not x.get('task_cards') for x in (daily,mixed))
            if args.ordinals:
                technical=await chat('解释TCP协议');ambiguous=await chat('它现在怎么样？')
                checks['technical_and_ambiguous_no_tasks']=not technical.get('task_cards') and not ambiguous.get('task_cards')
            original=await chat('第二台设备状态如何？' if args.ordinals else '帮我查询第二台设备的最新遥测、预测和当前告警，说明查询时间与数据时间。')
            sid=original['session_id'];assert original.get('task_cards'),original['warnings']
            tid=original['task_cards'][0]['task_id'];task=original['task_cards'][0]
            deadline=time.monotonic()+300
            while task['status'] not in ('succeeded','failed','cancelled','timeout') and time.monotonic()<deadline:
                await asyncio.sleep(2);reply=await request('GET','/api/v1/agent/tasks/'+tid);reply.raise_for_status();task=reply.json()['data']
            task=(await request('GET','/api/v1/agent/tasks/'+tid)).json()['data']
            records.append({'final_task':task})
            checks['real_dual_model_get']=task['status']=='succeeded' and task['model_calls']>0 and any(c['name']=='list_devices' for c in task['tool_calls'])
            checks['second_device_id_asc']=all(c['arguments'].get('device_id',second)==second for c in task['tool_calls'] if c['name'] in ('get_device','get_telemetry','get_prediction','list_alarms'))
            checks['current_evidence']=task['evidence']['state']=='queried' and bool(task['calls'])
            body={'parent_turn_id':original['turn_id'],'device_id':second,'checks':['latest_telemetry','prediction','current_alarms'],'verification_id':str(uuid4())}
            path='/api/v1/agent/sessions/'+sid+'/verify'
            verify=await request('POST',path,json=body);verify.raise_for_status();verified=verify.json()['data'];records.append({'verification':verified})
            repeated=(await request('POST',path,json=body)).json()['data']
            tasks=(await request('GET','/api/v1/agent/tasks',params={'session_id':sid})).json()['data']['list']
            reports=[(await request('GET','/api/v1/agent/tasks/'+str(t['task_id']))).json()['data'] for t in tasks]
            checks['fixed_verification_zero_models']=any(t['model_calls']==0 and t.get('verification_results') for t in reports)
            checks['verification_idempotent']=verified['turn_id']==repeated['turn_id']
            checks['alarms_complete']=verified['verification_results']['current_alarms']['complete'] is True
            history=(await request('GET','/api/v1/agent/sessions/'+sid)).json()['data'];records.append({'history':history})
            checks['history_append_and_link']=history['turns'][0]['response']['answer_markdown']==original['answer_markdown'] and task['parent_turn_id']==original['turn_id'] and len(history['turns'])==2
            explained=await chat('解释我的运维任务 '+tid+' 的结果，不需要重新查询。',sid)
            checks['explain_new_turn']=explained['turn_id']!=original['turn_id'] and not any(c['name']=='dispatch_ops_task' for c in explained['tool_calls'])
            checks['invalid_task_404']=(await request('GET','/api/v1/agent/tasks/'+str(uuid4()))).status_code==404
            checks['original_api_healthy']=(await client.get('http://127.0.0.1:8080/api/v1/devices')).status_code==200
            for source in verified.get('sources',[]):
                if source['kind']=='document': checks['source_read']=(await request('GET',source['url'])).status_code==200
            if args.ordinals:
                for index,question in enumerate(('帮我看看第二台现在怎么样','第二台有没有异常？'),1):
                    out=await chat(question);assert out.get('task_cards')
                    card=out['task_cards'][0];until=time.monotonic()+300
                    while card['status'] not in ('succeeded','failed','cancelled','timeout') and time.monotonic()<until:
                        await asyncio.sleep(2);card=(await request('GET','/api/v1/agent/tasks/'+card['task_id'])).json()['data']
                    records.append({'ordinal_task':card})
                    checks['ordinal_'+str(index)]=card['status']=='succeeded' and any(c['name']=='list_devices' for c in card['tool_calls']) and any(c['arguments'].get('device_id')==second for c in card['tool_calls'])
        finally:
            (args.output/'records.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
            (args.output/'checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(checks,ensure_ascii=False),flush=True)
    if not checks or not all(checks.values()): raise SystemExit('Acceptance incomplete or failed; preserve this batch.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--url',default='http://127.0.0.1:18081');parser.add_argument('--output',type=Path,required=True);parser.add_argument('--ordinals',action='store_true')
    asyncio.run(run(parser.parse_args()))
