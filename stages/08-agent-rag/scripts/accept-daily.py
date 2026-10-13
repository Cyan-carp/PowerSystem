"""BND-M2 acceptance of local or explicitly designated public entry; never logs credentials."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4
import httpx
from rag_agent.daily import SCOPE_REPLY, MIXED_REPLY


async def run(args):
    if args.base_url not in ('http://127.0.0.1:8080', 'http://localhost:8080', 'https://8.138.42.212'):
        raise ValueError('BND-M2 acceptance requires the local or designated public entry')
    if args.gate_health_url not in ('http://127.0.0.1:8093/health', 'http://agent-rag:8093/health'):
        raise ValueError('daily gate health probe must use the known local service')
    args.output.mkdir(parents=True,exist_ok=True)
    if args.program_only:
        async with httpx.AsyncClient(trust_env=False,timeout=5) as probe:
            health=(await probe.get(args.gate_health_url)).json()
            if health.get('boundaries',{}).get('daily_gate') is not True:
                raise RuntimeError('program-only acceptance requires the local daily gate enabled')
    rows=[]; checks={}
    async with httpx.AsyncClient(base_url=args.base_url,trust_env=False,timeout=135) as client:
        password=args.password_file.read_text(encoding='utf-8-sig').strip() if args.password_file else os.environ['NATURAL_TEST_PASSWORD']
        login=await client.post('/api/v1/auth/login',json={'username':args.username,'password':password})
        login.raise_for_status();client.headers['Authorization']='Bearer '+login.json()['data']['token']
        async def request(method,path,**kwargs):
            for _ in range(4):
                reply=await client.request(method,path,**kwargs)
                if reply.status_code!=429: return reply
                delay=min(60,int(reply.headers.get('Retry-After','60')))
                print('honoring local rate limit',delay,'seconds',flush=True)
                while delay:
                    await asyncio.sleep(min(5,delay));delay-=min(5,delay)
            return reply
        async def chat(label,question,mode='boundary',sid=None,required_tool=None):
            if args.program_only and mode not in ('boundary','preset'):
                raise RuntimeError('program-only mode cannot submit model requests')
            reply=await request('POST','/api/v1/agent/chat',json={'message':question,**({'session_id':sid} if sid else {})})
            data=reply.json().get('data',{}) if reply.status_code==200 else {}
            assertions={'http':reply.status_code==200,'mode':data.get('response_mode')==mode,
                        'status':data.get('status')==('out_of_scope' if mode=='boundary' else 'answered'),
                        'persisted':isinstance(data.get('turn_id'),int)}
            if mode in ('boundary','preset'):
                assertions.update(no_model=data.get('model_called') is False and data.get('model')=='',
                                  no_tools=data.get('tool_calls')==[] and data.get('sources')==[],
                                  no_verification=data.get('verification_available') is False)
                if mode=='boundary': assertions['fixed_text']=data.get('answer_markdown') in (SCOPE_REPLY,MIXED_REPLY)
            else:
                assertions['model_attempted']=data.get('model_called') is True
                assertions['answer']=bool(data.get('answer_markdown'))
            if required_tool:
                assertions['required_tool']=any(c['name']==required_tool and c['status']=='ok' for c in data.get('tool_calls',[]))
            row={'label':label,'question':question,'checks':assertions,'passed':all(assertions.values()),'response':data}
            rows.append(row)
            (args.output/'acceptance.json').write_text(json.dumps({'passed':all(r['passed'] for r in rows) and all(checks.values()),'cases':rows,'checks':checks},ensure_ascii=False,indent=2),encoding='utf-8')
            print(label,'PASS' if row['passed'] else 'FAIL',[k for k,v in assertions.items() if not v],flush=True)
            return data
        if args.history_file:
            before=json.loads(args.history_file.read_text(encoding='utf-8'))
            after=(await request('GET','/api/v1/agent/sessions/'+before['session_id'])).json()['data']
            checks['restart_history_unchanged']=after==before
        else:
            hello=await chat('greeting','你好','preset')
            life=await chat('food','西红柿炒蛋怎么做？')
            sid=life['session_id']
            await chat('thanks','谢谢','preset',sid)
            await chat('daily_followup','具体步骤呢？','boundary',sid)
            if not args.program_only:
                await chat('topic_change','你好，解释TCP协议。','model',sid)
            await chat('travel','帮我规划旅游行程')
            await chat('entertainment','讲个笑话')
            await chat('mixed','解释Redis，再推荐晚饭')
            if not args.program_only:
                await chat('knowledge','检索内部资料，说明本平台Docker服务组成。','model',required_tool='search_knowledge')
                await chat('web','请联网搜索最近一周光伏技术新闻，给出来源链接。','model',required_tool='web_search')
                device=await chat('device','第二台设备状态如何？','model')
                body={'parent_turn_id':device['turn_id'],'device_id':2,'checks':['latest_telemetry','current_alarms'],'verification_id':str(uuid4())}
                verification=await request('POST','/api/v1/agent/sessions/'+device['session_id']+'/verify',json=body)
                checks['bnd_m1_verification']=verification.status_code==200 and verification.json().get('data',{}).get('kind')=='verification'
                repeated=await request('POST','/api/v1/agent/sessions/'+device['session_id']+'/verify',json=body)
                checks['bnd_m1_idempotency']=repeated.status_code==200 and repeated.json().get('data',{}).get('turn_id')==verification.json().get('data',{}).get('turn_id')
                device_history=(await request('GET','/api/v1/agent/sessions/'+device['session_id'])).json()['data']
                checks['bnd_m1_original_preserved']=len(device_history['turns'])==2 and device_history['turns'][0]['response']['answer_markdown']==device['answer_markdown']
                source=next(s for row in rows for s in row['response'].get('sources',[]) if s['kind']=='document')
                checks['knowledge_source_readable']=(await request('GET','/api/v1/agent/rag/sources/'+source['id'])).status_code==200
            else:
                body={'parent_turn_id':life['turn_id'],'device_id':2,'checks':['latest_telemetry'],'verification_id':str(uuid4())}
            denied=await request('POST','/api/v1/agent/sessions/'+sid+'/verify',json={**body,'parent_turn_id':life['turn_id'],'verification_id':str(uuid4())})
            checks['program_parent_verification_denied']=denied.status_code==400
            history=(await request('GET','/api/v1/agent/sessions/'+sid)).json()['data']
            checks['expected_turns_saved']=len(history['turns'])==(3 if args.program_only else 4)
            checks['program_history_not_reenabled']=all(t['response'].get('verification_available') is False for t in history['turns'][:3])
            (args.output/'history-before.json').write_text(json.dumps(history,ensure_ascii=False,indent=2),encoding='utf-8')
            if args.admin_password_file:
                admin=await client.post('/api/v1/auth/login',json={'username':'p0-admin','password':args.admin_password_file.read_text(encoding='utf-8-sig').strip()})
                admin.raise_for_status()
                other={'Authorization':'Bearer '+admin.json()['data']['token']}
                path='/api/v1/agent/sessions/'+sid
                checks['cross_user_read_denied']=(await request('GET',path,headers=other)).status_code==404
                checks['cross_user_delete_denied']=(await request('DELETE',path,headers=other)).status_code==404
                checks['cross_user_verify_denied']=(await request('POST',path+'/verify',headers=other,json={**body,'parent_turn_id':life['turn_id'],'verification_id':str(uuid4())})).status_code==404
        for label,path in [('devices','devices?page_size=100'),('alarms','alarms?page_size=10'),('prediction','devices/2/prediction'),('statistics','alarms/statistics'),('dashboard','dashboard/summary'),('legacy_interpretations','agent/interpretations')]:
            reply=await request('GET','/api/v1/'+path)
            checks['business_'+label]=reply.status_code==200 and reply.json().get('code')==0
        if args.admin_password_file:
            client.headers.pop('Authorization')
            checks['anonymous_history_denied']=(await request('GET','/api/v1/agent/sessions')).status_code==401
            checks['registration_closed']=(await request('POST','/api/v1/auth/register',json={})).status_code==403
            checks['internal_blocked']=(await request('GET','/internal/agent/sessions')).status_code==403
        report={'passed':all(r['passed'] for r in rows) and all(checks.values()),'cases':rows,'checks':checks,
                'mode':'program-only' if args.program_only else 'full',
                'not_executed':['real_model','real_knowledge_tool','real_web_search','real_device_chat'] if args.program_only else []}
        name='restart.json' if args.history_file else 'acceptance.json'
        (args.output/name).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':report['passed'],'case_count':len(rows),'checks':checks},ensure_ascii=False),flush=True)
        return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://127.0.0.1:8080')
    parser.add_argument('--username',default=os.getenv('NATURAL_TEST_USERNAME','natural_test'))
    parser.add_argument('--password-file',type=Path)
    parser.add_argument('--admin-password-file',type=Path,help='existing admin credential for isolation/protection regression')
    parser.add_argument('--gate-health-url',default='http://127.0.0.1:8093/health')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--history-file',type=Path)
    parser.add_argument('--program-only',action='store_true',help='only fixed/preset requests; no model/tool queries')
    raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)
