"""Opt-in local S08 host; synthetic data and faults are never product entrypoints."""
import asyncio,hashlib,json,os,sys,time
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'artifacts/v4-智能助手/S08-定期任务-20261024-r1'
for p in ('08-agent-rag','09-ops-agent','10-security-agent'):sys.path.insert(0,str(ROOT/'stages'/p))
sys.path.insert(0,str(OUT/'runtime-deps'))
sys.path.insert(0,str(OUT/'runtime-deps-online'))
from zoneinfo import ZoneInfo
ZoneInfo('Asia/Shanghai')
import httpx,uvicorn
from fastapi import Request
DSN='postgresql://s08_test@127.0.0.1:25438/v4_s08_20261024_r1_test'
RAG='s08-local-synthetic-rag-token-000000000';OPS='s08-local-synthetic-ops-token-000000000';ACTOR='s08-local-synthetic-actor-key-000000000';ENVELOPE='s08-local-synthetic-envelope-key-000000'
SEC='s08-local-synthetic-sec-token-000000000';REQ='s08-local-synthetic-sec-request-0000000';RES='s08-local-synthetic-sec-result-00000000'
def candidate(role):
    reg=json.loads((ROOT/'artifacts/v4-智能助手/S03-持久任务-20261023-r1/candidate-registry.json').read_text(encoding='utf-8-sig'));value=next(x for x in reg['candidates'] if x['role']==role)
    assert value['endpoint']=='https://api.deepseek.com' and value['model']=='deepseek-flash' and value['thinking']=='disabled'
    return Path(value['credential_file']).read_text(encoding='utf-8-sig').strip()
def hashes():
    files=[]
    for d in ('08-agent-rag/rag_agent','09-ops-agent/ops_agent','10-security-agent/security_agent'):files.extend((ROOT/'stages'/d).glob('*.py'))
    files.extend([ROOT/'stages/02-backend/internal/api/agent_schedules.go',ROOT/'stages/02-backend/internal/api/s08_schedules_fixture_test.go',ROOT/'stages/08-agent-rag/deploy/005_schedules.sql'])
    return {str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
SOURCE=hashes()
def record(name,value):
    with (OUT/(name+'.jsonl')).open('a',encoding='utf8') as f:f.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(),**value),ensure_ascii=False)+'\n')
class ReviewClient:
    def __init__(self,client):self.client=client;self.mode='official'
    async def post(self,url,**kwargs):
        payload=kwargs['json'];projection=json.loads(payload['messages'][1]['content'])
        info=dict(role='stage10_security',mode=self.mode,model=payload['model'],thinking=payload['thinking'],projection=projection,source_hashes=SOURCE)
        if self.mode!='official':
            record('synthetic-review',info)
            if self.mode=='unavailable':return httpx.Response(503,json={})
            return httpx.Response(200,json=dict(choices=[dict(message=dict(content=json.dumps(dict(decision='deny' if self.mode=='deny' else 'allow',rule_codes=['scope_reviewed']))))],usage={}))
        try:
            response=await self.client.post(url,**kwargs)
            from security_agent.provider import read_message
            content,calls,usage=read_message(response);info.update(http_status=response.status_code,decision=json.loads(content),usage=usage,status='returned')
            return response
        except Exception as error:info.update(status='failed',error_type=type(error).__name__);raise
        finally:record('security-candidate',info)
class Assistant:
    def __init__(self,provider):self.provider=provider
    async def complete(self,messages,schemas=None):
        info=dict(role='stage8_assistant',model='deepseek-flash',thinking='disabled',source_hashes=SOURCE)
        try:
            value=await self.provider.complete(messages,schemas);info.update(status='returned',usage=value.get('usage'),tools=[x['function']['name'] for x in value.get('tool_calls',[])]);return value
        except Exception as error:info.update(status='failed',error_type=type(error).__name__);raise
        finally:record('assistant-candidate',info)
async def serve(role):
    client=httpx.AsyncClient(trust_env=False,follow_redirects=False)
    if role=='runs':
        from rag_agent.config import Config
        from rag_agent.store import Store
        from rag_agent.main import create_app
        from rag_agent.provider import Provider
        from rag_agent.tools import Tools
        cfg=Config(model='deepseek-flash',llm_url='https://api.deepseek.com',llm_key=candidate('stage8_assistant'),service_token=RAG,database_url=DSN,redis_url='redis://127.0.0.1:1',backend_url='http://127.0.0.1:28982',ops_url='http://127.0.0.1:28983',ops_token=OPS,ma_actor_key=ACTOR,ma_envelope_key=ENVELOPE,v4_assistant_enabled=True,v4_runs_enabled=True,v4_schedules_enabled=True,v4_artifacts_enabled=True,artifact_dir=OUT/'files',model_thinking='disabled',total_timeout=120,tool_timeout=35)
        store=await Store.open(DSN);repeat=await Store.open(DSN);await repeat.pool.close()
        app=create_app(cfg,store,object(),Assistant(Provider(cfg,client)),Tools(cfg,client,None));port=28981
        from rag_agent.run_worker import Runtime
        original_journal=Runtime.journal
        async def observed_journal(runtime,*args,**kwargs):
            try:return await original_journal(runtime,*args,**kwargs)
            except Exception as error:
                import traceback
                frames=[dict(file=Path(x.filename).name,line=x.lineno,function=x.name) for x in traceback.extract_tb(error.__traceback__)]
                record('execution-errors',dict(run_id=str(runtime.row['id']),error_type=type(error).__name__,message=cfg.redact(str(error)),frames=frames))
                raise
        Runtime.journal=observed_journal
        from rag_agent.schedules import ScheduleService
        original_valid=ScheduleService.valid;gate=dict(hold=False,arrived=False)
        async def gated_valid(service,sid,gid,c=None,lock=False):
            if lock and gate['hold']:
                gate['arrived']=True
                while gate['hold']:await asyncio.sleep(.05)
            return await original_valid(service,sid,gid,c,lock)
        ScheduleService.valid=gated_valid
        @app.post('/drill/delivery-gate')
        async def delivery_gate(req:Request):
            raw=await req.json()
            if 'hold' in raw:gate.update(hold=bool(raw['hold']),arrived=False)
            return gate
        @app.post('/drill/tick')
        async def tick(req:Request):
            raw=await req.json();now=datetime.fromisoformat(raw['now']) if raw.get('now') else None
            await asyncio.gather(*(app.state.schedules.tick(now) for _ in range(raw.get('concurrency',1))));return dict(ticked=True,synthetic_clock=bool(now))
    elif role=='authority':
        from ops_agent.config import Config
        from ops_agent.store import Store
        from ops_agent.main import create_app
        cfg=Config(database_url=DSN,redis_url='redis://127.0.0.1:1',service_token=OPS,actor_key=ACTOR,envelope_key=ENVELOPE,knowledge_token=RAG,backend_url='http://127.0.0.1:28982',supervisor_url='http://127.0.0.1:28981',security_url='http://127.0.0.1:28984',security_service_token=SEC,security_request_key=REQ,security_result_key=RES,v4_schedules_enabled=True,enabled=True,write_enabled=False)
        store=await Store.open(cfg);repeat=await Store.open(cfg);await repeat.pool.close()
        app=create_app(cfg,store,object(),run_worker=False);port=28983
    elif role=='security':
        from security_agent.config import Config
        from security_agent.main import create_app
        cfg=Config(SEC,REQ,RES,llm_key=candidate('stage10_security'),enabled=True,llm_url='https://api.deepseek.com',model='deepseek-flash',runtime_profile='v4-s08-local');app=create_app(cfg);port=28984
        review=ReviewClient(client)
        @app.middleware('http')
        async def inject(req,next_handler):app.state.action_client=review;return await next_handler(req)
        @app.post('/drill/mode')
        async def mode(req:Request):
            value=await req.json();assert value['mode'] in ('official','allow','deny','unavailable');review.mode=value['mode'];return dict(mode=review.mode)
    else:raise ValueError('invalid_role')
    @app.get('/drill/identity')
    async def identity():return dict(pid=os.getpid(),instance=os.environ['S08_INSTANCE'],role=role,synthetic=True,source_hashes=SOURCE)
    @app.post('/drill/shutdown')
    async def shutdown():server.should_exit=True;return dict(stopping=True)
    @app.middleware('http')
    async def audit(req,next_handler):
        response=await next_handler(req);record(role+'-http',dict(path=req.url.path,method=req.method,status=response.status_code));return response
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
    try:await server.serve()
    finally:
        await client.aclose()
        if role in ('runs','authority'):await store.pool.close()
if __name__=='__main__':asyncio.run(serve(sys.argv[1]))
