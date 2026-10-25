"""S03 isolated HTTP/process acceptance. Synthetic fixtures only; never production.

--serve is an explicit acceptance-only factory, not imported by production main.
--official uses only stage8_assistant from the owner-authorized candidate registry.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID,uuid4,uuid5

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'stages/08-agent-rag'))
import httpx
from rag_agent.config import Config
from rag_agent.provider import Provider
from rag_agent.store import Store
from rag_agent.main import create_app
from rag_agent.ops import SupervisorTools
from rag_agent.run_store import encode

DSN='postgresql://s03_test@127.0.0.1:25433/v4_s03_20261023_r1_test'
TOKEN='s03-synthetic-service-token-00000000'
URL='http://127.0.0.1:28931'
spec=importlib.util.spec_from_file_location('s02fixtures',Path(__file__).with_name('accept-v4-model.py'))
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


def config(official=False):
    cfg=Config(model='deepseek-flash',llm_url='https://api.deepseek.com',llm_key='',service_token=TOKEN,database_url=DSN,redis_url='redis://127.0.0.1:1',v4_assistant_enabled=True,v4_runs_enabled=True,model_thinking='disabled',total_timeout=120)
    if official:
        rows=json.loads((ROOT/'artifacts/v4-assistant/s02-20261023-r2/candidate-registry.json').read_text(encoding='utf-8-sig'))['candidates']
        row=next(r for r in rows if r['role']=='stage8_assistant')
        assert row['endpoint']=='https://api.deepseek.com' and row['model']=='deepseek-flash' and row['thinking']=='disabled'
        cfg.llm_key=Path(row['credential_file']).read_text(encoding='utf-8-sig').strip()
    return cfg


class DrillProvider:
    def __init__(self,store,delegate=None):self.store,self.delegate=store,delegate
    async def complete(self,messages,schemas=None):
        prompt=next(m['content'] for m in reversed(messages) if m['role']=='user')
        step='after_tool' if any(m['role']=='tool' for m in messages) else 'first'
        await self.store.pool.execute('INSERT INTO s03_drill_calls(question,step) VALUES($1,$2)',prompt,step)
        if os.getenv('S03_DRILL_HOLD')=='true' and step=='after_tool':await asyncio.sleep(45)
        if self.delegate:
            response=await self.delegate.complete(messages,schemas)
        elif step=='first':
            response=dict(role='assistant',content='',tool_calls=[dict(id='synthetic-call-1',type='function',function=dict(name='dispatch_ops_task',arguments=json.dumps(dict(intent='第二台设备当前状态，只读取证',operation='read_only'),ensure_ascii=False)))])
        else:response=dict(role='assistant',content='合成查询已完成，设备SYN-INV-0002的状态及采集时间见取证。')
        await self.store.pool.execute('INSERT INTO s03_drill_usage(usage) VALUES($1::jsonb)',encode(response.get('usage')))
        return response


class DrillTools:
    def __init__(self,cfg,store):self.cfg,self.store=cfg,store
    def run_tools(self,bound):
        # Exercise the real SupervisorTools and original-turn binding against a durable isolated child controller.
        outer=self
        class DrillOps(fixtures.FixtureOps):
            def __init__(self):
                super().__init__(outer.cfg,dict(id='Q02'))
                self.run_wait=120
            async def dispatch(self,intent,target=None,**kwargs):
                rid=bound.row['id'];tid=uuid5(rid,'synthetic-child')
                record=await outer.store.pool.fetchrow('SELECT result FROM s03_drill_tasks WHERE run_id=$1',rid)
                if record:return json.loads(record['result'])
                # Resolve explicit targets before explanatory mentions of another ordinal.
                fixture_intent=intent
                if target and re.search(r'第二(?:台|项)|第\s*2\s*(?:台|项)',target):fixture_intent='按台账id ASC第二项'
                elif re.search(r'第二(?:台|项)|第\s*2\s*(?:台|项)',intent) and '下一项' in intent:fixture_intent='按台账id ASC第二项'
                task=await super().dispatch(fixture_intent,target,**kwargs)
                task['synthetic_dispatch_intent']=intent
                task.update(task_id=str(tid),run_id=str(rid),parent_turn_id=bound.row['turn_id'],session_id=str(bound.row['session_id']))
                await outer.store.pool.execute('INSERT INTO s03_drill_tasks(run_id,task_id,turn_id,session_id,result) VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING',rid,tid,bound.row['turn_id'],bound.row['session_id'],encode(task))
                return task
        return SupervisorTools(fixtures.FixtureTools(self.cfg),DrillOps(),time.monotonic()+120)


async def serve(args):
    import uvicorn
    cfg=config(args.official)
    store=await Store.open(DSN)
    await store.pool.execute('''CREATE TABLE IF NOT EXISTS s03_drill_calls(id bigserial primary key,question text,step text);
      CREATE TABLE IF NOT EXISTS s03_drill_tasks(run_id uuid primary key,task_id uuid unique,turn_id bigint,session_id uuid,result jsonb);
      CREATE TABLE IF NOT EXISTS s03_drill_usage(id bigserial primary key,usage jsonb)''')
    client=httpx.AsyncClient(trust_env=False)
    provider=DrillProvider(store,Provider(cfg,client) if args.official else None)
    app=create_app(cfg,store,object(),provider,DrillTools(cfg,store))
    @app.get('/drill/identity')
    async def fixture_identity():return dict(pid=os.getpid(),official=args.official,instance=os.getenv('S03_DRILL_INSTANCE',''))
    try:await uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=28931,access_log=False,log_level='warning')).serve()
    finally:await client.aclose();await store.pool.close()


def headers(key=None):return {'X-PowerSystem-Token':TOKEN,'Authorization':'Bearer s03-http-synthetic-user-token','Idempotency-Key':str(key or uuid4())}


async def start(args,hold=False):
    async with httpx.AsyncClient(trust_env=False,timeout=.5) as check:
        try:
            response=await check.get(URL+'/health')
        except httpx.HTTPError:response=None
        if response is not None:raise RuntimeError('isolated fixture port already occupied; refusing concurrent service reuse')
    instance=uuid4().hex
    env=dict(os.environ,S03_DRILL_HOLD='true' if hold else 'false',S03_DRILL_INSTANCE=instance,PYTHONIOENCODING='utf-8')
    log=(args.output/('service-official.log' if args.official else 'service-deterministic.log')).open('a',encoding='utf-8')
    command=[sys.executable,str(Path(__file__).resolve()),'--serve','--output',str(args.output)]
    if args.official:command.append('--official')
    p=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));log.close()
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(100):
            if p.poll() is not None:raise RuntimeError('isolated service failed; see owned service log')
            try:
                response=await client.get(URL+'/drill/identity')
                if response.status_code==200 and response.json().get('instance')==instance:
                    p.actual_pid=response.json()['pid']
                    return p
            except httpx.HTTPError:pass
            await asyncio.sleep(.1)
    p.terminate();raise RuntimeError('service startup timeout')


async def stop(p):
    # Windows venv redirectors can own a different PID than the actual interpreter.
    if getattr(p,'actual_pid',p.pid)!=p.pid:
        try:os.kill(p.actual_pid,signal.SIGTERM)
        except ProcessLookupError:pass
    if p.poll() is None:p.terminate()
    await asyncio.to_thread(p.wait,10)


async def snapshot(client,rid,user):
    r=await client.get(URL+'/internal/agent/runs/'+rid,params=dict(user_id=user),headers=headers());r.raise_for_status();return r.json()


async def completed(client,rid,user,timeout=125):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        result=await snapshot(client,rid,user)
        if result['status'] in ('succeeded','partial','failed','cancelled'):return result
        await asyncio.sleep(.05)
    raise TimeoutError('run completion timeout')


async def driver(args):
    args.output.mkdir(parents=True,exist_ok=True)
    store=await Store.open(DSN)
    run_user=1900000000+int(uuid4().hex[:5],16)
    usage_start=await store.pool.fetchval('SELECT coalesce(max(id),0) FROM s03_drill_usage') if await store.pool.fetchval("SELECT to_regclass('s03_drill_usage') IS NOT NULL") else 0
    report=dict(mode='official' if args.official else 'deterministic',started_at=datetime.now(timezone.utc).isoformat(),database=DSN,concurrency=1,samples=[],checks={},failures=[],profile=config(False).provider_profile(),cost=dict(amount=None,reason='actual account billing not inspected; usage retained, no guessed rates'))
    p=None
    try:
        if not args.official:
            p=await start(args,hold=True)
            async with httpx.AsyncClient(trust_env=False,timeout=10) as c:
                key=uuid4();question='第二台设备状态如何？S03隔离进程恢复合成测试'
                admitted=await c.post(URL+'/internal/agent/runs',json=dict(user_id=run_user,message=question),headers=headers(key));admitted.raise_for_status();receipt=admitted.json();rid=receipt['run_id']
                async with c.stream('GET',URL+'/internal/agent/runs/'+rid+'/events',params={'user_id':run_user},headers=headers()) as stream:
                    async for line in stream.aiter_lines():
                        if line.startswith('id: '):break
                # Subscriber explicitly disconnected while the model/tool operation continues.
                for _ in range(200):
                    if await store.pool.fetchval('SELECT count(*) FROM assistant_run_steps WHERE run_id=$1 AND kind=\'read_tool\' AND state=\'completed\'',UUID(rid)):break
                    await asyncio.sleep(.05)
                else:raise AssertionError('no completed tool checkpoint before kill')
                assert (await snapshot(c,rid,run_user))['status']=='running'
                await stop(p);p=None
                p=await start(args,hold=False)
                result=await completed(c,rid,run_user)
                assert result['status']=='succeeded',result
                rows=await store.pool.fetch('SELECT step_key,kind,state,attempts FROM assistant_run_steps WHERE run_id=$1 ORDER BY step_key',UUID(rid))
                assert next(r for r in rows if r['step_key']=='model:0')['attempts']==1
                assert next(r for r in rows if r['kind']=='read_tool')['attempts']==1
                assert await store.pool.fetchval('SELECT count(*) FROM s03_drill_tasks WHERE run_id=$1',UUID(rid))==1
                child=await store.pool.fetchrow('SELECT * FROM s03_drill_tasks WHERE run_id=$1',UUID(rid))
                assert child['turn_id']==receipt['turn_id'] and str(child['session_id'])==receipt['session_id']
                duplicate=await c.post(URL+'/internal/agent/runs',json=dict(user_id=run_user,message=question),headers=headers(key))
                assert duplicate.json()['run_id']==rid
                events=await c.get(URL+'/internal/agent/runs/'+rid+'/events',params=dict(user_id=run_user),headers=headers())
                parsed=[json.loads(line[6:]) for line in events.text.splitlines() if line.startswith('data: ')]
                assert [e['seq'] for e in parsed]==list(range(1,result['last_seq']+1))
                assert parsed[-1]==result['terminal_event']
                report['checks'].update(real_tcp_disconnect=True,worker_service_process_terminated=True,lease_recovered=True,completed_model_not_replayed=True,child_task_count=1,child_bound_at_creation=True,original_turn_restored=True,lost_receipt_retry_same_run=True,snapshot_matches_terminal_event=True)
                report['restart_reference']=dict(receipt=receipt,steps=[dict(r) for r in rows],snapshot=result)
                write(args.output/'restart-reference.json',report['restart_reference'])
        else:p=await start(args)
        if args.official:
            async with httpx.AsyncClient(trust_env=False,timeout=10) as c:
                for index in range(30):
                    begin=time.perf_counter()
                    response=await c.post(URL+'/internal/agent/runs',json=dict(user_id=run_user,message='第二台设备状态如何？请说明当前遥测、预测和告警，并注明数据时间。'),headers=headers())
                    admitted_ms=(time.perf_counter()-begin)*1000
                    sample=dict(index=index+1,admission_ms=round(admitted_ms,2),http_status=response.status_code)
                    if response.status_code==202:
                        receipt=response.json();sample['receipt']=receipt
                        result=await completed(c,receipt['run_id'],run_user)
                        sample.update(completion_ms=round((time.perf_counter()-begin)*1000,2),snapshot=result)
                        sample['question_complete']=result['status']=='succeeded' and 'SYN-INV-0002' in result['final_answer'] and bool(result['result_refs'])
                    report['samples'].append(sample)
                    if not sample.get('question_complete'):report['failures'].append(index+1)
                    write(args.output/'official-latency.partial.json',report)
                    print(encode(dict(sample=index+1,completed=sample.get('question_complete'),admission_ms=sample['admission_ms'],completion_ms=sample.get('completion_ms'))),flush=True)
            def stats(key):
                values=sorted(s[key] for s in report['samples'] if key in s)
                return dict(samples=len(values),p50_ms=values[math.ceil(.5*len(values))-1],p95_ms=values[math.ceil(.95*len(values))-1]) if values else dict(samples=0)
            report['admission']=stats('admission_ms');report['completion']=stats('completion_ms')
            report['target_passed']=not report['failures'] and report['admission']['p95_ms']<=1000 and report['completion']['p95_ms']<=20000
            report['usage']=[json.loads(r['usage']) for r in await store.pool.fetch('SELECT usage FROM s03_drill_usage WHERE id>$1 ORDER BY id',usage_start) if r['usage']]
            report['usage_scope']='current invocation only; no account billing assertion'
        report['finished_at']=datetime.now(timezone.utc).isoformat()
    except Exception as e:
        report['failures'].append(type(e).__name__+': '+str(e)[:500]);raise
    finally:
        if p:await stop(p)
        report['owned_service_stopped']=True
        report['source_hashes']={str(path.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(path.read_bytes()).hexdigest() for path in (ROOT/'stages/08-agent-rag/rag_agent').glob('*.py')}
        report['environment_boundary']='real local PostgreSQL + real HTTP/process lifecycle; child controller/business evidence are synthetic; official mode adds the real owner-authorized API. No production/business write capability.'
        write(args.output/('official-latency.json' if args.official else 'process-validation.json'),report)
        await store.pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');parser.add_argument('--official',action='store_true');parser.add_argument('--output',type=Path,default=ROOT/'artifacts/v4-assistant/s03-20261023-r1')
    args=parser.parse_args()
    asyncio.run(serve(args) if args.serve else driver(args))
