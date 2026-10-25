"""Opt-in local S07 dependency host. Never imported by product entrypoints."""
import asyncio,hashlib,json,os,sys,time
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'artifacts/v4-assistant/s07-20261024-r1'
for p in ('08-agent-rag','09-ops-agent','10-security-agent'):sys.path.insert(0,str(ROOT/'stages'/p))
import httpx,uvicorn
from fastapi import Request

DSN='postgresql://s07_test@127.0.0.1:25437/v4_s07_20261024_r1_test'
RAG_TOKEN='s07-local-synthetic-rag-token-000000000'
OPS_TOKEN='s07-local-synthetic-ops-token-000000000'
ACTOR='s07-local-synthetic-actor-key-000000000'
ENVELOPE='s07-local-synthetic-envelope-key-000000'
SEC_TOKEN='s07-local-synthetic-sec-token-000000000'
SEC_REQUEST='s07-local-synthetic-sec-request-0000000'
SEC_RESULT='s07-local-synthetic-sec-result-00000000'

def candidate(role):
    reg=json.loads((ROOT/'artifacts/v4-assistant/s03-20261023-r1/candidate-registry.json').read_text(encoding='utf-8-sig'))
    value=next(x for x in reg['candidates'] if x['role']==role)
    assert value['endpoint']=='https://api.deepseek.com' and value['model']=='deepseek-flash' and value['thinking']=='disabled'
    return Path(value['credential_file']).read_text(encoding='utf-8-sig').strip()

def source_hashes():
    paths=[]
    for directory in ('08-agent-rag/rag_agent','09-ops-agent/ops_agent','09-ops-agent/ops_executor','10-security-agent/security_agent'):
        paths.extend((ROOT/'stages'/directory).glob('*.py'))
    paths += [ROOT/'stages/02-backend/internal/api'/p for p in ('business_actions.go','agent_actions.go','s07_ops_fixture_test.go')]
    return {str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

def record(role,value):
    with (OUT/(role+'-calls.jsonl')).open('a',encoding='utf8') as f:f.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(),**value),ensure_ascii=False)+'\n')

BOOT_SOURCE_HASHES=source_hashes()

class Assistant:
    def __init__(self,real):self.real=real
    async def complete(self,messages,schemas=None):
        q=next(m['content'] for m in reversed(messages) if m['role']=='user')
        if q.startswith('官方:'):
            invocation=dict(source_hashes=BOOT_SOURCE_HASHES,message_sha256=hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest(),model='deepseek-flash',thinking='disabled',role='stage8_assistant')
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
            parts=q.removeprefix('慢').strip().split(' ')
            cap={'诊断':'service.diagnose','日志':'service.logs','证据':'service.evidence','重启':'service.restart'}[parts[0]]
            name='ops_action';args=dict(capability=cap,asset_id=parts[1])
        return dict(role='assistant',content='',tool_calls=[dict(id='s07-fixed-call',type='function',function=dict(name=name,arguments=json.dumps(args,ensure_ascii=False)))])

class ReviewClient:
    def __init__(self,real):self.real=real;self.mode='allow'
    async def post(self,url,**kwargs):
        body=kwargs['json'];projected=json.loads(body['messages'][1]['content'])
        if self.mode=='official':
            invocation=dict(source_hashes=BOOT_SOURCE_HASHES,projection=projected,model=body['model'],thinking=body['thinking'],role='stage10_security')
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
        cfg=Config(model='deepseek-flash',llm_url='https://api.deepseek.com',llm_key=candidate('stage8_assistant'),service_token=RAG_TOKEN,database_url=DSN,redis_url='redis://127.0.0.1:1',backend_url='http://127.0.0.1:28972',ops_url='http://127.0.0.1:28973',ops_token=OPS_TOKEN,ma_actor_key=ACTOR,ma_envelope_key=ENVELOPE,v4_assistant_enabled=True,v4_runs_enabled=True,v4_ops_enabled=True,v4_artifacts_enabled=True,artifact_dir=OUT/'files',model_thinking='disabled',total_timeout=120,tool_timeout=35)
        store=await Store.open(DSN);repeat=await Store.open(DSN);await repeat.pool.close()
        app=create_app(cfg,store,object(),Assistant(Provider(cfg,client)),Tools(cfg,client,None));port=28971
    elif role=='authority':
        from ops_agent.config import Config
        from ops_agent.store import Store
        from ops_agent.main import create_app
        cfg=Config(database_url=DSN,redis_url='redis://127.0.0.1:1',service_token=OPS_TOKEN,actor_key=ACTOR,envelope_key=ENVELOPE,backend_url='http://127.0.0.1:28972',supervisor_url='http://127.0.0.1:28971',security_url='http://127.0.0.1:28974',security_service_token=SEC_TOKEN,security_request_key=SEC_REQUEST,security_result_key=SEC_RESULT,v4_ops_enabled=True,enabled=True,write_enabled=False,executor_url='http://127.0.0.1:28975',executor_token='s07-local-adapter-token-000000000000000',executor_write_key='s07-local-adapter-grant-000000000000000')
        store=await Store.open(cfg);repeat=await Store.open(cfg);await repeat.pool.close()
        app=create_app(cfg,store,object(),run_worker=False);port=28973
        @app.post('/drill/expiry-race')
        async def expiry_race():
            app.state.expire_after_validation=True;return dict(enabled=True,synthetic=True)
        @app.middleware('http')
        async def setup_race(req,next_handler):
            authority=app.state.service_authority
            if not getattr(authority,'race_installed',False):
                original=authority.validate_plan
                async def validate(task,approval,jwt):
                    await original(task,approval,jwt)
                    if approval['status']=='approved' and getattr(app.state,'expire_after_validation',False):
                        app.state.expire_after_validation=False
                        await authority.pool.execute("UPDATE ma_approvals SET expires_at=now()-interval '1 second' WHERE task_id=$1",task['task_id'])
                authority.validate_plan=validate;authority.race_installed=True
            return await next_handler(req)
    elif role=='security':
        from security_agent.config import Config
        from security_agent.main import create_app
        cfg=Config(SEC_TOKEN,SEC_REQUEST,SEC_RESULT,llm_key=candidate('stage10_security'),enabled=True,llm_url='https://api.deepseek.com',model='deepseek-flash',runtime_profile='v4-s07-local')
        app=create_app(cfg);port=28974
        review_client=ReviewClient(client)
        @app.middleware('http')
        async def synthetic_control(req,next_handler):app.state.action_client=review_client;return await next_handler(req)
        @app.post('/drill/mode')
        async def mode(request:Request):
            value=await request.json();assert value['mode'] in ('allow','deny','unavailable','require_approval','official');review_client.mode=value['mode'];return dict(mode=review_client.mode,synthetic=True)
    elif role=='adapter':
        from ops_executor.service_adapter import ServiceConfig,create_app
        cfg=ServiceConfig(OUT/'adapter-config.json');app=create_app(cfg);port=28975
        app.state.fault='none'
        @app.post('/drill/fault')
        async def fault(request:Request):
            value=await request.json();assert value['mode'] in ('none','health-fail','drop-result','unresolved','fail-before','pause')
            app.state.fault=value['mode']
            if value['mode']=='none':(cfg.service.root/'unhealthy').unlink(missing_ok=True)
            return dict(mode=app.state.fault,synthetic=True)
        async def before_restart():
            if app.state.fault=='health-fail':(cfg.service.root/'unhealthy').touch()
        async def after_receipt(receipt):
            if app.state.fault in ('drop-result','unresolved'):raise RuntimeError('synthetic_response_loss_after_real_receipt')
        app.state.before_restart=before_restart;app.state.after_receipt=after_receipt
        @app.middleware('http')
        async def fault_transport(req,next_handler):
            if req.url.path.endswith('/execute'):
                app.state.arrived=True
                if app.state.fault=='pause':
                    for _ in range(200):
                        if app.state.fault!='pause':break
                        await asyncio.sleep(.05)
                if app.state.fault=='fail-before':return __import__('fastapi').responses.JSONResponse(status_code=409,content={'detail':'synthetic_pre_execution_failure'})
            if '/receipts/' in req.url.path and app.state.fault in ('unresolved','drop-result'):return __import__('fastapi').responses.JSONResponse(status_code=503,content={'detail':'synthetic_receipt_unavailable'})
            return await next_handler(req)
    else:raise ValueError(role)
    @app.get('/drill/identity')
    async def identity():return dict(pid=os.getpid(),instance=os.getenv('S07_INSTANCE'),role=role,synthetic=True,source_hashes=BOOT_SOURCE_HASHES)
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
        if role in ('runs','authority'):await store.pool.close()

if __name__=='__main__':asyncio.run(serve(sys.argv[1]))
