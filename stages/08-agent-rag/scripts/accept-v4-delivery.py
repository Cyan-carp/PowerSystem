"""S05 opt-in local delivery host. Never imported by production.

Real Store.open migrations and workers. Synthetic fixed-tool and official
candidate branches share the actual business_report adapter and file storage.
"""
import asyncio, hashlib, json, os, sys, time
from pathlib import Path
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'stages/08-agent-rag'))
import httpx, uvicorn
from fastapi import Request
from rag_agent.config import Config
from rag_agent.main import create_app
from rag_agent.provider import Provider
from rag_agent.store import Store
from rag_agent.tools import Tools

OUT=ROOT/'artifacts/v4-智能助手/S05-业务分析-20261023-r1'
DSN='postgresql://s05_test@127.0.0.1:25435/v4_s05_20261023_r1_test'
SERVICE='s05-local-synthetic-service-token-00000000'


def source_hashes():
    paths=list((ROOT/'stages/08-agent-rag/rag_agent').glob('*.py'))
    paths += [ROOT/'stages/02-backend/internal/api'/x for x in ('energy_report.go','agent_artifacts.go','s05_delivery_fixture_test.go')]
    paths += [Path(__file__)]
    return {str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


class AcceptanceProvider:
    def __init__(self,real): self.real=real
    async def complete(self,messages,schemas=None):
        question=next(m['content'] for m in reversed(messages) if m['role']=='user')
        if question.startswith('官方:'):
            invocation=dict(at=datetime.now(timezone.utc).isoformat(),source_hashes=source_hashes(),message_sha256=hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest(),provider='official DeepSeek',model='deepseek-flash',thinking='disabled')
            try:
                answer=await self.real.complete(messages,schemas)
                invocation.update(status='returned',usage=answer.get('usage'),tool_names=[c['function']['name'] for c in answer.get('tool_calls',[])])
                return answer
            except Exception as e:
                invocation.update(status='failed',error_type=type(e).__name__)
                raise
            finally:
                with (OUT/'provider-calls.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(invocation,ensure_ascii=False)+'\n')
        last=max(i for i,m in enumerate(messages) if m['role']=='user')
        if any(m['role']=='tool' for m in messages[last+1:]): return dict(role='assistant',content='实际结果以本轮权威报表及产物为准。')
        if '慢' in question: await asyncio.sleep(5)
        elif '中速' in question: await asyncio.sleep(2)
        kind=next((x for x in ('alarm-timeliness','availability','investigation','telemetry') if x in question),'energy')
        params=dict(kind=kind)
        if kind=='energy': params.update(start_date='2026-10-20',end_date='2026-10-22',scope='business',group_by='device' if 'device' in question else 'station')
        else:params.update(start='2026-10-20T00:00:00Z',end='2026-10-21T00:00:00Z')
        if kind=='telemetry':params['device_id']=501
        if '超限' in question:params['end_date']='2026-12-22'
        if '空报表' in question:params['device_id']=999999
        return dict(role='assistant',content='',tool_calls=[dict(id='s05-report',type='function',function=dict(name='business_report',arguments=json.dumps(params)))])


async def serve():
    cfg=Config(model='deepseek-flash',llm_url='https://api.deepseek.com',llm_key='',service_token=SERVICE,database_url=DSN,redis_url='redis://127.0.0.1:1',backend_url='http://127.0.0.1:28952',v4_assistant_enabled=True,v4_runs_enabled=True,v4_artifacts_enabled=True,artifact_dir=OUT/'files',model_thinking='disabled',total_timeout=120)
    registry=json.loads((ROOT/'artifacts/v4-智能助手/S03-持久任务-20261023-r1/candidate-registry.json').read_text(encoding='utf-8-sig'))
    candidate=next(r for r in registry['candidates'] if r['role']=='stage8_assistant')
    assert candidate['endpoint']==cfg.llm_url and candidate['model']==cfg.model and candidate['thinking']=='disabled'
    cfg.llm_key=Path(candidate['credential_file']).read_text(encoding='utf-8-sig').strip()
    store=await Store.open(DSN)
    # Exercise the same startup migration entry twice for idempotence.
    repeat=await Store.open(DSN);await repeat.pool.close()
    client=httpx.AsyncClient(trust_env=False)
    app=create_app(cfg,store,object(),AcceptanceProvider(Provider(cfg,client)),Tools(cfg,client,None))
    audit=[]
    @app.middleware('http')
    async def observed(request,call_next):
        response=await call_next(request)
        if request.url.path.startswith('/internal/agent'):
            audit.append(dict(path=request.url.path,method=request.method,status=response.status_code,at=datetime.now(timezone.utc).isoformat()))
            (OUT/'http-audit.json').write_text(json.dumps(audit,indent=2),encoding='utf8')
        return response
    @app.get('/drill/identity')
    async def identity(): return dict(pid=os.getpid(),instance=os.getenv('S05_INSTANCE'),synthetic=True)
    @app.post('/drill/shutdown')
    async def shutdown():server.should_exit=True;return dict(stopping=True)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=28951,log_level='warning'))
    try:await server.serve()
    finally:await client.aclose();await store.pool.close()

if __name__=='__main__':asyncio.run(serve())
