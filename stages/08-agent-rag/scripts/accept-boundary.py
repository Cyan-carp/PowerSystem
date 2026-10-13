"""Authenticated acceptance of the specified public entry. Secrets stay in memory."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import httpx


async def run(args):
    assert args.base_url in ('https://8.138.42.212','http://127.0.0.1:8080')
    args.output.mkdir(parents=True,exist_ok=True)
    checks={}; records=[]
    async with httpx.AsyncClient(base_url=args.base_url,trust_env=False,timeout=135) as client:
        login=await client.post('/api/v1/auth/login',json={'username':'demo-operator','password':args.password_file.read_text().strip()})
        login.raise_for_status();client.headers['Authorization']='Bearer '+login.json()['data']['token']
        async def request(method,path,**kwargs):
            for attempt in range(3):
                reply=await client.request(method,path,**kwargs)
                if reply.status_code!=429: return reply
                delay=min(60,int(reply.headers.get('Retry-After','60')))
                print('rate limited; waiting',delay,'seconds',flush=True)
                await asyncio.sleep(delay)
            return reply
        async def chat(text,sid=None,expected='answered'):
            out=await request('POST','/api/v1/agent/chat',json={'message':text,**({'session_id':sid} if sid else {})})
            assert out.status_code==200,out.status_code
            data=out.json()['data'];records.append({'question':text,'response':data})
            assert data['status']==expected and data['answer_markdown'] and data['turn_id']>0
            calls=[c for c in data['tool_calls'] if c['name'] not in ('web_search','search_knowledge')]
            success=sum(c['status']=='ok' for c in calls)
            expected='not_queried' if not calls else 'queried' if success==len(calls) else 'partial_failed' if success else 'failed'
            assert data['evidence']['state']==expected
            assert not any(k in data for k in ('source_coverage_percent','verified_facts','out_of_scope'))
            for card in data['evidence']['cards']:
                source=next(c for c in calls if c['name']==card['tool'] and c['collected_at']==card['queried_at'])
                if card['tool']=='get_telemetry' and source['status']=='ok':
                    assert card['received_at']==source['result'].get('received_at') and card['ts_ms']==source['result'].get('ts_ms')
            print('chat passed:',text,expected,flush=True)
            return data
        if args.history_file:
            before=json.loads(args.history_file.read_text()); history=(await request('GET','/api/v1/agent/sessions/'+before['session_id'])).json()['data']
            canonical=json.dumps(history,ensure_ascii=False,sort_keys=True)
            checks['restart_history_unchanged']=hashlib.sha256(canonical.encode()).hexdigest()==before['sha256']
        else:
            questions=['第二台设备状态如何？','帮我看看第二台现在怎么样','第二台有没有异常？']
            for index,text in enumerate(questions):
                original=await chat(text);sid=original['session_id']
                body={'parent_turn_id':original['turn_id'],'device_id':2,'checks':['latest_telemetry','current_alarms','prediction'],'verification_id':str(uuid4())}
                path='/api/v1/agent/sessions/'+sid+'/verify'
                reply=await request('POST',path,json=body);assert reply.status_code==200,reply.text
                verification=reply.json()['data'];records.append({'verification':verification})
                checks['device_'+str(index)]=verification['device']['device_code']=='INV-1002' and verification['parent_turn_id']==original['turn_id']
                assert verification['kind']=='verification' and verification['verification_results']['latest_telemetry']['status']=='ok'
                assert verification['verification_results']['current_alarms']['complete'] is True
                repeated=(await request('POST',path,json=body)).json()['data']
                checks['idempotent_'+str(index)]=repeated['turn_id']==verification['turn_id'] and repeated['tool_calls']==verification['tool_calls']
                history=(await request('GET','/api/v1/agent/sessions/'+sid)).json()['data']
                checks['append_'+str(index)]=len(history['turns'])==2 and history['turns'][0]['response']['answer_markdown']==original['answer_markdown'] and history['turns'][1]['parent_turn_id']==original['turn_id']
                if index==0:
                    follow=await chat('它呢？再说明一下。',sid)
                    checks['multi_turn']=follow['session_id']==sid
                    history=(await request('GET','/api/v1/agent/sessions/'+sid)).json()['data']
                    canonical=json.dumps(history,ensure_ascii=False,sort_keys=True)
                    (args.output/'history-before.json').write_text(json.dumps({'session_id':sid,'sha256':hashlib.sha256(canonical.encode()).hexdigest()},indent=2))
                    admin=await client.post('/api/v1/auth/login',json={'username':'p0-admin','password':args.admin_password_file.read_text().strip()})
                    admin.raise_for_status();other={'Authorization':'Bearer '+admin.json()['data']['token']}
                    checks['cross_user_verify_denied']=(await request('POST',path,json={**body,'verification_id':str(uuid4())},headers=other)).status_code==404
                    checks['cross_user_read_denied']=(await request('GET','/api/v1/agent/sessions/'+sid,headers=other)).status_code==404
                    checks['cross_user_delete_denied']=(await request('DELETE','/api/v1/agent/sessions/'+sid,headers=other)).status_code==404
            life=await chat('西红柿炒蛋怎么做？',expected='out_of_scope');checks['life_boundary']=life.get('response_mode')=='boundary' and life.get('model_called') is False and not life['tool_calls'] and not life['sources'] and life['verification_available'] is False
            tech=await chat('请通俗地解释TCP，给一个例子。');checks['technical_unrestricted']=len(tech['answer_markdown'])>80
            internal=await chat('检索内部资料，说明本平台Docker服务组成。');checks['knowledge_regression']=any(c['name']=='search_knowledge' and c['status']=='ok' for c in internal['tool_calls'])
            web=await chat('请用联网搜索查询最近一周光伏技术新闻并给出来源链接。');checks['search_regression']=any(c['name']=='web_search' and c['status']=='ok' for c in web['tool_calls']) and any(s['kind']=='web' for s in web['sources'])
        for label,path in [('devices','/api/v1/devices?page_size=100'),('alarms','/api/v1/alarms?page_size=10'),('prediction','/api/v1/devices/2/prediction'),('statistics','/api/v1/alarms/statistics'),('interpretations','/api/v1/agent/interpretations'),('dashboard','/api/v1/dashboard/summary')]:
            reply=await request('GET',path);checks['legacy_'+label]=reply.status_code==200 and reply.json().get('code')==0
        (args.output/('restart.json' if args.history_file else 'acceptance.json')).write_text(json.dumps({'passed':all(checks.values()),'checks':checks,'records':records},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':all(checks.values()),'checks':checks},ensure_ascii=False),flush=True)
        return all(checks.values())


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base-url',required=True);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--password-file',required=True,type=Path);p.add_argument('--admin-password-file',required=True,type=Path);p.add_argument('--history-file',type=Path)
    raise SystemExit(0 if asyncio.run(run(p.parse_args())) else 1)
