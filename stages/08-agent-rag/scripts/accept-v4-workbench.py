"""S04 local browser acceptance host. Not imported by production.

Real Store.open migrations, real run worker, HTTP/SSE; business data is synthetic.
Fault-control routes only exist in this opt-in loopback host/dedicated database.
"""
import asyncio,importlib.util,json,os,sys,argparse,hashlib,time
from pathlib import Path
from uuid import UUID,uuid4
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'stages/08-agent-rag'))
import httpx,uvicorn
from fastapi import Request,HTTPException
from fastapi.responses import StreamingResponse,JSONResponse
from rag_agent.main import create_app
from rag_agent.store import Store
from rag_agent.config import Config
from rag_agent.tools import Tools
from rag_agent.provider import Provider
from rag_agent.run_store import unpack,encode
DSN='postgresql://s04_test@127.0.0.1:25434/v4_s04_20261023_r1_test'
OUT=ROOT/'artifacts/v4-assistant/s04-20261023-r1'
SERVICE='s04-local-synthetic-service-token-00000000'

class BrowserProvider:
    def __init__(self,delegate):self.delegate=delegate;self.calls=[]
    async def complete(self,messages,schemas=None):
        prompt=next(m['content'] for m in reversed(messages) if m['role']=='user')
        self.calls.append({'question':prompt,'at':datetime.now(timezone.utc).isoformat()})
        if prompt.startswith('官方:'):
            response=await self.delegate.complete(messages,schemas)
            self.calls[-1]['usage']=response.get('usage');self.calls[-1]['official']=True
            return response
        last_user=max(i for i,m in enumerate(messages) if m['role']=='user')
        if '工具' in prompt and not any(m['role']=='tool' for m in messages[last_user+1:]):
            return dict(role='assistant',content='',tool_calls=[dict(id='s04-read-call',type='function',function=dict(name='search_knowledge',arguments='{"query":"S04合成来源"}'))])
        if '失败场景' in prompt:raise RuntimeError('synthetic provider unavailable')
        delay=8 if '慢' in prompt else 2 if '中速' in prompt else .5
        if '取消' in prompt or '预算' in prompt:delay=20
        await asyncio.sleep(delay)
        text=f'本轮目标：{prompt}。合成数据只用于工作台验收。'
        if '长正文' in prompt:text+='\n\n'+('\n\n### 观测记录\n\n设备数据与采集窗口仅作合成布局验收，缺测不能当作零。'+('长字段'*50))*35
        return dict(role='assistant',content=text)

class BrowserTools(Tools):
    async def execute(self,name,arguments,jwt,result):
        if name!='search_knowledge':return await super().execute(name,arguments,jwt,result)
        await asyncio.sleep(3)
        data=dict(id=2,device_code='SYN-S04-0002',name='S04合成设备',synthetic=True)
        result['tool_calls'].append(dict(name=name,arguments=json.loads(arguments),status='ok',result=data,collected_at=datetime.now(timezone.utc).isoformat()))
        result['sources'].append(dict(id='K'+'a'*20,kind='document',title='S04合成来源',url='/api/v1/agent/rag/sources/K'+'a'*20+'?collection=powersystem_rag_s04fixture&version='+'b'*64,version='b'*64,collected_at=datetime.now(timezone.utc).isoformat()))
        return data

async def serve():
    cfg=Config(model='deepseek-flash',llm_url='https://api.deepseek.com',llm_key='',service_token=SERVICE,database_url=DSN,redis_url='redis://127.0.0.1:1',v4_assistant_enabled=True,v4_runs_enabled=True,model_thinking='disabled',total_timeout=120)
    rows=json.loads((ROOT/'artifacts/v4-assistant/s03-20261023-r1/candidate-registry.json').read_text(encoding='utf-8-sig'))['candidates']
    row=next(r for r in rows if r['role']=='stage8_assistant')
    assert row['endpoint']==cfg.llm_url and row['model']==cfg.model and row['thinking']=='disabled'
    cfg.llm_key=Path(row['credential_file']).read_text(encoding='utf-8-sig').strip()
    store=await Store.open(DSN)
    client=httpx.AsyncClient(trust_env=False)
    provider=BrowserProvider(Provider(cfg,client))
    app=create_app(cfg,store,object(),provider,BrowserTools(cfg,client,None))
    fault={'drop_receipt':False,'duplicate_events':False}
    audit=json.loads((OUT/'http-audit.json').read_text(encoding='utf8')) if (OUT/'http-audit.json').exists() else []
    @app.post('/drill/shutdown')
    async def shutdown():server.should_exit=True;return {'stopping':True}
    @app.post('/drill/faults')
    async def faults(request:Request):fault.update(await request.json());return fault
    @app.middleware('http')
    async def transport_faults(request,call_next):
        if request.url.path.startswith('/internal/agent') and time.monotonic()<fault.get('network_until',0):
            audit.append(dict(path=request.url.path,method=request.method,status=503,synthetic_network_fault=True,at=datetime.now(timezone.utc).isoformat()))
            (OUT/'http-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf8')
            return JSONResponse({'detail':'synthetic_network_interruption'},status_code=503)
        if request.method=='GET' and '/internal/agent/runs/' in request.url.path and not request.url.path.endswith('/events') and fault.get('fail_snapshot_count',0)>0:
            fault['fail_snapshot_count']-=1
            return JSONResponse({'detail':'synthetic_snapshot_unavailable'},status_code=503)
        response=await call_next(request)
        if request.method=='POST' and request.url.path=='/internal/agent/runs' and fault.get('delay_receipt_seconds'):
            delay=fault.pop('delay_receipt_seconds');await asyncio.sleep(delay)
        if request.method=='GET' and '/internal/agent/runs/' in request.url.path and not request.url.path.endswith('/events') and fault.get('delay_snapshot_expire'):
            fault['delay_snapshot_expire']=False
            await asyncio.sleep(4)
            rid=UUID(request.url.path.rsplit('/',1)[-1])
            await store.pool.execute("UPDATE assistant_run_events SET at=now()-interval '8 days' WHERE run_id=$1",rid)
        if request.url.path.startswith('/internal/agent'):
            audit.append(dict(path=request.url.path,query=str(request.url.query),method=request.method,status=response.status_code,idempotency_key=request.headers.get('Idempotency-Key'),at=datetime.now(timezone.utc).isoformat()))
            (OUT/'http-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf8')
        if request.method=='POST' and request.url.path=='/internal/agent/runs' and (fault['drop_receipt'] or fault.get('drop_receipt_count',0)>0):
            fault['drop_receipt']=False
            fault['drop_receipt_count']=max(0,fault.get('drop_receipt_count',0)-1)
            async def broken():
                async for _ in response.body_iterator:pass
                yield b'{"run_id":' # incomplete payload, genuinely lost receipt after durable admission
            return StreamingResponse(broken(),status_code=202,media_type='application/json')
        if request.url.path.endswith('/events') and fault['duplicate_events'] and response.status_code==200:
            fault['duplicate_events']=False
            async def duplicate():
                async for chunk in response.body_iterator:
                    yield chunk;yield chunk
            return StreamingResponse(duplicate(),media_type='text/event-stream')
        if request.url.path.endswith('/events') and fault.get('disconnect_event') and response.status_code==200:
            target=fault.pop('disconnect_event')
            async def interrupt():
                async for chunk in response.body_iterator:
                    yield chunk
                    if ('"type": "'+target+'"') in (chunk.decode() if isinstance(chunk,bytes) else chunk):
                        fault['network_until']=time.monotonic()+6
                        return
            return StreamingResponse(interrupt(),media_type='text/event-stream')
        return response
    @app.get('/drill/identity')
    async def identity():return dict(pid=os.getpid(),instance=os.getenv('S04_INSTANCE',''),synthetic=True)
    @app.post('/drill/seed')
    async def seed():
        sid=uuid4()
        await store.ensure(sid,40001,'S04旧历史原文',create=True)
        result=dict(request_id='legacy-s04',session_id=str(sid),status='answered',answer_markdown='旧历史原文：S04-LEGACY-UNCHANGED',sources=[],tool_calls=[],warnings=[],model='historical-model')
        await store.pool.execute("INSERT INTO rag_turns(session_id,question,response,model_messages) VALUES($1,'旧问题原文',$2::jsonb,'[]')",sid,encode(result))
        return dict(session_id=str(sid),sha256=hashlib.sha256(encode(result).encode()).hexdigest())
    @app.post('/drill/{rid}/control')
    async def control(rid:UUID,request:Request):
        body=await request.json();op=body.get('op')
        row=unpack(await store.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1',rid))
        if op=='expire':
            await store.pool.execute("UPDATE assistant_run_events SET at=now()-interval '8 days' WHERE run_id=$1",rid)
        elif op=='budget':
            await store.pool.execute("UPDATE assistant_runs SET deadline=now()-interval '1 second' WHERE id=$1",rid)
        elif op in ('partial','reconciling','awaiting_approval','metadata','release'):
            # State-display fixtures: authoritative persistence and real snapshot/event route,
            # no actual write/approval/artifact feature is created or licensed.
            if op!='release' and row['status'] not in ('succeeded','failed','partial','cancelled'):raise HTTPException(409,'wait_for_terminal_fixture')
            actions=[];artifacts=[]
            if op=='metadata':
                actions=[dict(action_id='synthetic-expired-action',status='expired',effect_summary='合成过期审批',expires_at='2026-01-01T00:00:00Z',stale=True)]
                artifacts=[dict(artifact_id='synthetic-expired-artifact',name='合成过期产物.csv',status='expired',mime_type='text/csv',size=42,expires_at='2026-01-01T00:00:00Z'),dict(artifact_id='synthetic-unavailable-artifact',name='合成不可用产物.csv',status='unavailable',mime_type='text/csv')]
            if op=='reconciling':actions=[dict(action_id='synthetic-uncertain-action',status='execution_uncertain',effect_summary='合成未知结果，仅验证显示',stale=True)]
            async with store.pool.acquire() as c,c.transaction():
                row=unpack(await c.fetchrow('SELECT * FROM assistant_runs WHERE id=$1 FOR UPDATE',rid))
                row.update(status='partial' if op=='release' else op if op!='metadata' else row['status'],phase=op,actions=actions,artifacts=artifacts,requires_manual_reconciliation=op in ('partial','release'))
                if op in ('reconciling','awaiting_approval'):await c.execute("UPDATE assistant_runs SET lease_until=now()+interval '1 hour' WHERE id=$1",rid)
                await app.state.runs._event(c,row,'progress',dict(phase=op,message='合成状态展示夹具'))
        elif op=='source':
            result=json.loads(await store.pool.fetchval('SELECT response FROM rag_turns WHERE id=$1',row['turn_id']))
            result['sources']=[dict(id='K'+'a'*20,kind='document',title='S04合成来源',url='/api/v1/agent/rag/sources/K'+'a'*20+'?collection=powersystem_rag_s04fixture&version='+'b'*64,version='b'*64,collected_at=datetime.now(timezone.utc).isoformat())]
            await store.pool.execute('UPDATE rag_turns SET response=$2::jsonb WHERE id=$1',row['turn_id'],encode(result))
        else:raise HTTPException(400,'unknown_drill_operation')
        return await app.state.runs.snapshot(rid,40001)
    class SourceFixture:
        async def source(self,chunk,collection=None,version=None):
            if collection!='powersystem_rag_s04fixture' or version!='b'*64:raise ValueError('fixed_reference_required')
            return dict(document='S04合成来源',heading='固定版本段落',content='来源版本已固定；本内容仅为合成浏览器夹具。',version=version,page=1)
    @app.middleware('http')
    async def attach_source(request,call_next):
        if '/rag/sources/' in request.url.path:app.state.knowledge=SourceFixture()
        return await call_next(request)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=28941,access_log=False,log_level='warning',timeout_graceful_shutdown=2))
    try:await server.serve()
    finally:
        (OUT/'provider-calls.json').write_text(json.dumps(provider.calls,ensure_ascii=False,indent=2),encoding='utf8')
        await client.aclose();await store.pool.close()

if __name__=='__main__':asyncio.run(serve())
