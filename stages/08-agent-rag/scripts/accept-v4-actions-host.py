"""Opt-in local S06 dependency host. Never imported by product entrypoints."""
import asyncio,hashlib,json,os,sys,time
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'artifacts/v4-智能助手/S06-安全审查-20261024-r1'
for p in ('08-agent-rag','09-ops-agent','10-security-agent'):sys.path.insert(0,str(ROOT/'stages'/p))
import httpx,uvicorn
from fastapi import Request

DSN='postgresql://s06_test@127.0.0.1:25436/v4_s06_20261024_r1_test'
RAG_TOKEN='s06-local-synthetic-rag-token-000000000'
OPS_TOKEN='s06-local-synthetic-ops-token-000000000'
ACTOR='s06-local-synthetic-actor-key-000000000'
ENVELOPE='s06-local-synthetic-envelope-key-000000'
SEC_TOKEN='s06-local-synthetic-sec-token-000000000'
SEC_REQUEST='s06-local-synthetic-sec-request-0000000'
SEC_RESULT='s06-local-synthetic-sec-result-00000000'

def candidate(role):
    reg=json.loads((ROOT/'artifacts/v4-智能助手/S03-持久任务-20261023-r1/candidate-registry.json').read_text(encoding='utf-8-sig'))
    value=next(x for x in reg['candidates'] if x['role']==role)
    assert value['endpoint']=='https://api.deepseek.com' and value['model']=='deepseek-flash' and value['thinking']=='disabled'
    return Path(value['credential_file']).read_text(encoding='utf-8-sig').strip()

def source_hashes():
    paths=[]
    for directory in ('08-agent-rag/rag_agent','09-ops-agent/ops_agent','10-security-agent/security_agent'):
        paths.extend((ROOT/'stages'/directory).glob('*.py'))
    paths += [ROOT/'stages/02-backend/internal/api'/p for p in ('business_actions.go','agent_actions.go','s06_actions_fixture_test.go')]
    return {str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

def record(role,value):
    with (OUT/(role+'-calls.jsonl')).open('a',encoding='utf8') as f:f.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(),**value),ensure_ascii=False)+'\n')

class Assistant:
    def __init__(self,real):self.real=real
    async def complete(self,messages,schemas=None):
        q=next(m['content'] for m in reversed(messages) if m['role']=='user')
        if q.startswith('官方:'):
            invocation=dict(source_hashes=source_hashes(),message_sha256=hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest(),model='deepseek-flash',thinking='disabled',role='stage8_assistant')
            try:
                result=await self.real.complete(messages,schemas);invocation.update(status='returned',usage=result.get('usage'),tools=[c['function']['name'] for c in result.get('tool_calls',[])]);return result
            except Exception as e:invocation.update(status='failed',error_type=type(e).__name__);raise
            finally:record('assistant',invocation)
        if any(m['role']=='tool' for m in messages[max(i for i,m in enumerate(messages) if m['role']=='user')+1:]):return dict(role='assistant',content='结果以权威记录为准。')
        if q.startswith('慢'):await asyncio.sleep(8)
        if '普通读取' in q:
            name='business_report';args=dict(kind='energy',start_date='2026-10-23',end_date='2026-10-24',scope='all',device_id=601,deliver=False)
        elif '自然解释' in q:return dict(role='assistant',content='告警确认表示人员已看到告警，不能据此判断故障已经修复。')
        else:
            parts=q.removeprefix('慢').strip().split(' ',2)
            cap={'备注':'device.remark','读取备注':'device.remark.read','确认告警':'alarm.ack'}[parts[0]]
            target=int(parts[1]);args=dict(alarm_id=target) if cap=='alarm.ack' else dict(device_id=target)
            if cap=='device.remark':
                # Human explicitly includes the bound version in the synthetic command.
                text=parts[2].split(' ',1);args.update(expected_version=int(text[0]),remark=text[1] if len(text)>1 else '')
            name='business_action';args=dict(capability=cap,typed_args=args)
        return dict(role='assistant',content='',tool_calls=[dict(id='s06-fixed-call',type='function',function=dict(name=name,arguments=json.dumps(args,ensure_ascii=False)))])

class ReviewClient:
    def __init__(self,real):self.real=real;self.mode='allow'
    async def post(self,url,**kwargs):
        body=kwargs['json'];projected=json.loads(body['messages'][1]['content'])
        if self.mode=='official':
            invocation=dict(source_hashes=source_hashes(),projection=projected,model=body['model'],thinking=body['thinking'],role='stage10_security')
            try:
                response=await self.real.post(url,**kwargs)
                from security_agent.provider import read_message
                content,calls,usage=read_message(response)
                invocation.update(status='returned',usage=usage,decision=json.loads(content),http_status=response.status_code)
                return response
            except Exception as e:invocation.update(status='failed',error_type=type(e).__name__);raise
            finally:record('security',invocation)
        record('synthetic-review',dict(mode=self.mode,projection=projected,candidate=False))
        if self.mode=='unavailable':return httpx.Response(503,json={'detail':'synthetic_dependency_unavailable'})
        decision='deny' if self.mode=='deny' else 'require_approval' if self.mode=='require_approval' else 'allow'
        return httpx.Response(200,json=dict(choices=[dict(message=dict(content=json.dumps(dict(decision=decision,rule_codes=['scope_denied' if decision=='deny' else 'scope_reviewed']))))],usage={}))

async def serve(role):
    client=httpx.AsyncClient(trust_env=False,follow_redirects=False)
    if role=='runs':
        from rag_agent.config import Config
        from rag_agent.store import Store
        from rag_agent.main import create_app
        from rag_agent.provider import Provider
        from rag_agent.tools import Tools
        cfg=Config(model='deepseek-flash',llm_url='https://api.deepseek.com',llm_key=candidate('stage8_assistant'),service_token=RAG_TOKEN,database_url=DSN,redis_url='redis://127.0.0.1:1',backend_url='http://127.0.0.1:28962',ops_url='http://127.0.0.1:28963',ops_token=OPS_TOKEN,ma_actor_key=ACTOR,ma_envelope_key=ENVELOPE,v4_assistant_enabled=True,v4_runs_enabled=True,v4_actions_enabled=True,v4_artifacts_enabled=True,artifact_dir=OUT/'files',model_thinking='disabled',total_timeout=120,tool_timeout=35)
        store=await Store.open(DSN);repeat=await Store.open(DSN);await repeat.pool.close()
        app=create_app(cfg,store,object(),Assistant(Provider(cfg,client)),Tools(cfg,client,None));port=28961
    elif role=='authority':
        from ops_agent.config import Config
        from ops_agent.store import Store
        from ops_agent.main import create_app
        cfg=Config(database_url=DSN,redis_url='redis://127.0.0.1:1',service_token=OPS_TOKEN,actor_key=ACTOR,envelope_key=ENVELOPE,backend_url='http://127.0.0.1:28962',supervisor_url='http://127.0.0.1:28961',security_url='http://127.0.0.1:28964',security_service_token=SEC_TOKEN,security_request_key=SEC_REQUEST,security_result_key=SEC_RESULT,v4_actions_enabled=True,enabled=True,write_enabled=False)
        store=await Store.open(cfg);repeat=await Store.open(cfg);await repeat.pool.close()
        app=create_app(cfg,store,object(),run_worker=False);port=28963
    elif role=='security':
        from security_agent.config import Config
        from security_agent.main import create_app
        cfg=Config(SEC_TOKEN,SEC_REQUEST,SEC_RESULT,llm_key=candidate('stage10_security'),enabled=True,llm_url='https://api.deepseek.com',model='deepseek-flash',runtime_profile='v4-s06-local')
        app=create_app(cfg);port=28964
        review_client=ReviewClient(client)
        @app.middleware('http')
        async def synthetic_control(req,next_handler):app.state.action_client=review_client;return await next_handler(req)
        @app.post('/drill/mode')
        async def mode(request:Request):
            value=await request.json();assert value['mode'] in ('allow','deny','unavailable','require_approval','official');review_client.mode=value['mode'];return dict(mode=review_client.mode,synthetic=True)
    else:raise ValueError(role)
    @app.get('/drill/identity')
    async def identity():return dict(pid=os.getpid(),instance=os.getenv('S06_INSTANCE'),role=role,synthetic=True,source_hashes=source_hashes())
    @app.post('/drill/shutdown')
    async def shutdown():server.should_exit=True;return dict(stopping=True)
    @app.middleware('http')
    async def audit(req,next_handler):
        response=await next_handler(req)
        record(role+'-http',dict(path=req.url.path,method=req.method,status=response.status_code))
        return response
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
    try:await server.serve()
    finally:
        await client.aclose()
        if role!='security':await store.pool.close()

if __name__=='__main__':asyncio.run(serve(sys.argv[1]))
