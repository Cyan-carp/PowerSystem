import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4
import httpx
import pytest
import pytest_asyncio
from ops_agent.config import Config
from ops_agent.schemas import Task, Verification
from ops_agent.security import canonical, digest, sign, verify, claims
from ops_agent.store import Store, LostLease
from ops_agent.worker import Worker
from ops_agent.executor import SCHEMAS, MANIFEST, fixed_verification
from ops_agent.main import create_app


def task(user=91001, sid=None, key=None, **extra):
    return Task(protocol="ma/v1", task_id=uuid4(), trace_id=uuid4(), idempotency_key=key or uuid4(), session_id=sid or uuid4(),
                actor={"user_id":user,"role":"operator"}, intent="查第二台设备", user_request="第二台现在怎么样", created_at=datetime.now(timezone.utc), **extra)


@pytest_asyncio.fixture
async def env(tmp_path):
    if not os.getenv("OPS_DATABASE_URL"): pytest.skip("independent MA test database not configured")
    cfg=Config.load()
    if not cfg.database_url.split("?")[0].endswith("_test"): raise RuntimeError("test_database_required")
    cfg.audit_dir=tmp_path;cfg.enabled=True
    store=await Store.open(cfg)
    async def cleanup():
        # Only these dedicated test tables; never truncate business or ordinary chat history.
        await store.pool.execute("TRUNCATE ma_execution_attempts,ma_approvals,ma_policy_versions,ma_tool_calls,ma_task_steps,ma_task_events,ma_outbox,ma_tasks,ma_session_guards")
    await cleanup()
    yield cfg,store
    await cleanup();await store.pool.close()


class Queue:
    def __init__(self, fail=False): self.messages=[];self.fail=fail
    async def ping(self):
        if self.fail: raise OSError()
        return True
    async def xadd(self,stream,data):
        if self.fail: raise OSError()
        self.messages.append(data);return str(len(self.messages))
    async def xack(self,*args): return 1


def backend(cfg, paths, many=False):
    def handler(req):
        paths.append((req.method,req.url.path))
        if req.url.path=="/internal/ma/v1/actor":
            user=int(req.headers["authorization"].split("-")[-1])
            return httpx.Response(200,json={"data":{"context":sign(claims({"user_id":user,"role":"operator"},"",uuid4(),""),cfg.actor_key)}})
        assert req.method=="GET"
        p=req.url.path
        if p=="/api/v1/devices": data={"list":[{"id":10,"device_code":"A"},{"id":30,"device_code":"B"}],"total":2}
        elif p.endswith("/telemetry/latest"): data={"device_id":30,"device_code":"B","status":1,"ts_ms":1000,"received_at":"2026-10-12T00:00:00Z"}
        elif p.endswith("/prediction"): data={"device_id":30,"probability":.9,"threshold":.8,"stale":True,"window_end_ms":1000}
        elif p=="/api/v1/alarms":
            status=req.url.params.get("status","unhandled");data={"list":[{"device_id":30,"status":status}]*100 if many else [],"total":250 if many else 0}
        elif p=="/api/v1/alarms/statistics": data={"complete":True,"total_events":100}
        elif p=="/api/v1/alarms/1": data={"id":1,"status":"recovered"}
        elif p=="/api/v1/dashboard/summary": data={"device_total":2}
        elif p.endswith("/telemetry"): data={"points":[[1000,1.2]]}
        else: data={"id":30,"device_code":"B","name":"fixture"}
        return httpx.Response(200,json={"code":0,"data":data})
    return handler


async def execute(env, body=None, provider=None, verification=None, many=False):
    cfg,store=env;body=body or task();tid,_=await store.create(body);row=await store.claim(tid,"fixture")
    paths=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend(cfg,paths,many))) as client:
        contexts={tid:{"jwt":"token-"+str(body.actor.user_id),"verification":verification}}
        worker=Worker(cfg,store,Queue(),client,contexts,provider)
        await worker.run_task(row)
    return await store.report(tid,body.actor.user_id),paths


def call(name,args,cid="c1"):
    return {"role":"assistant","content":None,"tool_calls":[{"id":cid,"function":{"name":name,"arguments":json.dumps(args)}}]}


@pytest.mark.asyncio
async def test_MA_C01_supervisor_scope_and_clarification(env):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"08-agent-rag"))
    from rag_agent.ops import SupervisorTools
    assert {s["function"]["name"] for s in SupervisorTools.schemas}=={"search_knowledge","web_search","dispatch_ops_task","get_ops_task"}
    async def provider(*args):return {"role":"assistant","content":'{"clarification":"请选择设备"}'}
    report,paths=await execute(env,provider=provider)
    assert report["diagnosis"]["clarification"] and not paths and not report["steps"]


def test_MA_C02_protocol_and_signature():
    t=task();raw=t.model_dump(mode="json")
    for change in ({"protocol":"ma/v2"},{"actor":{"user_id":True,"role":"admin"}},{"extra":"bad"},{"constraints":{"max_risk_class":"R2"}}):
        with pytest.raises(Exception):Task.model_validate({**raw,**change})
    c=claims({"user_id":1,"role":"operator"},uuid4(),uuid4(),"hash")
    token=sign(c,"a"*32);assert verify(token,"a"*32,"ma-ops")==c
    with pytest.raises(ValueError):verify(token+"a","a"*32,"ma-ops")
    with pytest.raises(ValueError):verify(token,"a"*32,"ma-envelope")
    with pytest.raises(ValueError):verify(sign({**c,"exp":0},"a"*32),"a"*32,"ma-ops")


@pytest.mark.asyncio
async def test_MA_C03_ordinal_requires_actual_sorted_inventory(env):
    replies=[call("platform__list_devices",{}),call("platform__get_telemetry",{"device_id":30}),{"role":"assistant","content":'{"summary":"B是ID升序第二项"}'}]
    async def provider(messages,schemas):
        assert all("restart" not in s["function"]["name"] for s in schemas)
        return replies.pop(0)
    report,paths=await execute(env,provider=provider)
    assert report["status"]=="succeeded" and report["model_calls"]==3
    assert ("GET","/api/v1/devices/30/telemetry/latest") in paths
    assert report["evidence"]["state"]=="queried" and report["evidence"]["cards"][-1]["ts_ms"]==1000


@pytest.mark.asyncio
async def test_MA_C04_all_eight_get_routes(env):
    cfg,store=env;t=task();await store.create(t);row=await store.claim(t.task_id,"fixture");epoch=row["execution_epoch"]
    await store.transition(t.task_id,epoch,"executing")
    paths=[];result={"tool_calls":[],"sources":[],"warnings":[]}
    args=[("list_devices",{}),("get_device",{"device_id":30}),("get_telemetry",{"device_id":30}),("get_prediction",{"device_id":30}),("list_alarms",{}),("get_alarm_statistics",{}),("get_alarm_detail",{"alarm_id":1}),("get_dashboard_summary",{})]
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend(cfg,paths))) as client:
        worker=Worker(cfg,store,Queue(),client,{})
        for step,(name,arg) in enumerate(args,1):
            data=await worker.executor.call(row,epoch,step,"platform."+name,arg,"token-91001",result)
            assert "error" not in data
    assert len(result["tool_calls"])==8 and all(method=="GET" for method,_ in paths)
    assert result["tool_calls"][0]["result"]["order"]=="id ASC"


@pytest.mark.asyncio
async def test_MA_C05_guard_blocks_unknown_and_invalid_before_http(env):
    cfg,store=env;paths=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend(cfg,paths))) as client:
        worker=Worker(cfg,store,Queue(),client,{})
        for name,args in [("docker.restart",{}),("platform.get_device",{"device_id":True}),("platform.list_devices",{"command":"x"})]:
            with pytest.raises(ValueError):await worker.executor.call({},0,1,name,args,"",{})
    assert not paths


@pytest.mark.asyncio
async def test_MA_C06_idempotent_acceptance_and_conflict(env):
    _,store=env;t=task();assert (await store.create(t))[1]
    same=Task.model_validate({**t.model_dump(mode="json"),"task_id":str(uuid4()),"trace_id":str(uuid4())})
    assert await store.create(same)==(t.task_id,False)
    same.intent="different"
    with pytest.raises(Exception) as e:await store.create(same)
    assert e.value.status_code==409


@pytest.mark.asyncio
async def test_MA_C07_claim_race_and_duplicate_notice(env):
    _,store=env;t=task();await store.create(t)
    rows=await asyncio.gather(store.claim(t.task_id,"one"),store.claim(t.task_id,"two"))
    assert sum(r is not None for r in rows)==1


@pytest.mark.asyncio
async def test_MA_C08_outbox_recovers_after_delivery_crash(env):
    cfg,store=env;t=task();await store.create(t);queue=Queue(True)
    worker=Worker(cfg,store,queue,None,{})
    await worker.delivery();assert await store.pool.fetchval("SELECT count(*) FROM ma_outbox WHERE NOT delivered")==1
    queue.fail=False;await store.pool.execute("UPDATE ma_outbox SET next_attempt_at=now()")
    await worker.delivery();assert queue.messages==[{"protocol":"ma/v1","task_id":str(t.task_id)}]
    await store.pool.execute("UPDATE ma_outbox SET delivered=false")
    await worker.delivery();assert len(queue.messages)==2
    assert sum(r is not None for r in await asyncio.gather(store.claim(t.task_id,"one"),store.claim(t.task_id,"two")))==1


@pytest.mark.asyncio
async def test_MA_C09_ownership_all_routes_and_signed_identity(env):
    cfg,store=env;app=create_app(cfg,store,Queue(),run_worker=False)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        t=task();await store.create(t)
        def headers(user):return {"X-PowerSystem-Token":cfg.service_token,"X-MA-Actor":sign(claims({"user_id":user,"role":"admin"},t.session_id,t.trace_id,""),cfg.actor_key)}
        url="/internal/ma/v1/tasks/"+str(t.task_id)
        assert (await client.get(url,headers=headers(91001))).status_code==200
        assert (await client.get(url,headers=headers(91002))).status_code==404
        assert (await client.post(url+"/cancel",headers=headers(91002))).status_code==404
        assert (await client.get(url,headers={**headers(91001),"X-MA-Actor":"forged"})).status_code==403
        assert not (await client.get('/internal/ma/v1/tasks',headers=headers(91002))).json()['list']
        bind={'session_id':str(t.session_id),'parent_turn_id':123}
        h=headers(91002);h['X-MA-Envelope']=sign({'aud':'ma-envelope','kid':'ma-v1','iat':int(time.time()),'exp':int(time.time())+450,'hash':digest({'task_id':str(t.task_id),**bind})},cfg.envelope_key)
        assert (await client.post(url+'/bind-turn',headers=h,json=bind)).status_code==404


@pytest.mark.asyncio
async def test_MA_C10_acceptance_has_no_success_evidence(env):
    _,store=env;t=task();await store.create(t);report=await store.report(t.task_id,91001)
    assert report['status']=='queued' and report['evidence']['state']=='not_queried'
    assert not report['steps'] and report['model_calls']==0


@pytest.mark.asyncio
async def test_MA_C11_persistent_report_and_late_binding(env):
    cfg,store=env;t=task();await store.create(t)
    fresh=await Store.open(cfg)
    assert (await fresh.report(t.task_id,91001))['link_pending']
    await fresh.bind(t.task_id,91001,t.session_id,123);await fresh.bind(t.task_id,91001,t.session_id,123)
    with pytest.raises(Exception):await fresh.bind(t.task_id,91001,t.session_id,124)
    assert (await fresh.report(t.task_id,91001))['parent_turn_id']==123
    await fresh.pool.close()


@pytest.mark.asyncio
async def test_MA_C12_fixed_verification_zero_models(env):
    t=task();v=Verification(session_id=t.session_id,parent_turn_id=5,device_id=30,checks=['current_alarms','latest_telemetry','prediction'],verification_id=uuid4())
    async def forbidden(*args):pytest.fail('verification must not call model')
    report,_=await execute(env,t,forbidden,v)
    assert report['status']=='succeeded' and report['model_calls']==0 and report['verification_results']['current_alarms']['complete']


@pytest.mark.asyncio
async def test_MA_C13_verification_incomplete_and_idempotent(env):
    cfg,store=env;cfg.verify_max_pages=1;t=task();v=Verification(session_id=t.session_id,parent_turn_id=5,device_id=30,checks=['current_alarms'],verification_id=t.idempotency_key)
    report,_=await execute(env,t,verification=v,many=True)
    assert report['status']=='failed' and not report['verification_results']['current_alarms']['complete']
    assert await store.create(t)==(t.task_id,False)


def test_MA_C14_daily_gate_zero_ops():
    from rag_agent.daily import classify
    for question in ['你好','推荐晚饭','解释TCP，顺便推荐晚饭']:
        assert classify(question).action!='allow'
    assert classify('解释TCP').action=='allow'


@pytest.mark.asyncio
async def test_MA_C15_redaction_and_injected_tool_data(env):
    cfg,store=env;t=task();t.user_request='Bearer abcdef-secret sk-'+'x'*32
    await store.create(t);stored=await store.get(t.task_id)
    assert 'abcdef-secret' not in json.dumps(stored['envelope']) and 'sk-' not in json.dumps(stored['envelope'])
    assert all(m['risk_class']=='R0' for m in MANIFEST.values())
    text=cfg.redact(' '.join([cfg.llm_key,cfg.actor_key,cfg.envelope_key,'Bearer injected-secret']))
    assert cfg.actor_key not in text and 'injected-secret' not in text
    row=await store.claim(t.task_id,'fixture')
    await store.transition(t.task_id,row['execution_epoch'],'failed',{'diagnosis':{'summary':cfg.llm_key+' Bearer injected-secret'},'warnings':[]})
    persisted=json.dumps((await store.get(t.task_id))['result'])
    assert cfg.llm_key not in persisted and 'injected-secret' not in persisted



@pytest.mark.asyncio
async def test_MA_C16_user_global_queue_and_budget_limits(env):
    cfg,store=env;t=task();await store.create(t)
    with pytest.raises(Exception) as e:await store.create(task())
    assert e.value.status_code==429
    cfg.queue_capacity=2;await store.create(task(user=91002))
    with pytest.raises(Exception) as e:await store.create(task(user=91003))
    assert e.value.status_code==429
    cfg.queue_capacity=20;third=task(user=91003);await store.create(third)
    assert await store.claim(t.task_id,'a');second=await store.pool.fetchval('SELECT task_id FROM ma_tasks WHERE user_id=91002')
    assert await store.claim(second,'b');assert await store.claim(third.task_id,'c') is None
    assert (cfg.task_timeout,cfg.model_timeout,cfg.tool_timeout,cfg.max_rounds,cfg.max_calls)==(300,60,15,8,16)
    cfg.max_calls=17
    with pytest.raises(ValueError):cfg.validate()


@pytest.mark.asyncio
async def test_MA_C17_restart_auth_loss_and_epoch_rejection(env):
    _,store=env;t=task();await store.create(t);await store.recover(startup=True)
    assert (await store.report(t.task_id,91001))['error']['code']=='auth_context_lost'
    fresh=task(user=91002);await store.create(fresh);r=await store.claim(fresh.task_id,'a')
    with pytest.raises(LostLease):await store.check(fresh.task_id,r['execution_epoch']+1)
    await store.pool.execute("UPDATE ma_tasks SET lease_until=now()-interval '1 second' WHERE task_id=$1",fresh.task_id)
    await store.recover();assert (await store.report(fresh.task_id,91002))['error']['code']=='worker_interrupted'


@pytest.mark.asyncio
async def test_MA_C18_cancellation_and_atomic_delete_guard(env):
    _,store=env;t=task();await store.create(t);op=uuid4()
    with pytest.raises(Exception):await store.guard(t.session_id,91001,op,'reserve')
    assert (await store.cancel(t.task_id,91001))['status']=='cancelled'
    assert (await store.cancel(t.task_id,91001))['status']=='cancelled'
    await store.guard(t.session_id,91001,op,'reserve')
    with pytest.raises(Exception):await store.create(task(sid=t.session_id))
    await store.guard(t.session_id,91001,op,'commit')
    with pytest.raises(Exception):await store.create(task(sid=t.session_id))
    other=task(user=91002);await store.create(other);r=await store.claim(other.task_id,'a');await store.cancel(other.task_id,91002)
    with pytest.raises(InterruptedError):await store.check(other.task_id,r['execution_epoch'])


@pytest.mark.asyncio
async def test_MA_C19_model_failure_keeps_program_evidence(env):
    calls=0
    async def provider(*args):
        nonlocal calls;calls+=1
        if calls==1:return call('platform__get_telemetry',{'device_id':30})
        raise ValueError('model_http_503')
    report,_=await execute(env,provider=provider)
    assert report['status']=='failed' and report['tool_calls'][0]['result']['ts_ms']==1000
    assert report['error']['code']=='model_http_503'


@pytest.mark.asyncio
async def test_retry_success_preserves_both_attempts(env):
    cfg,store=env;t=task();await store.create(t);row=await store.claim(t.task_id,'fixture');paths=[];attempts=0
    normal=backend(cfg,paths)
    def handler(req):
        nonlocal attempts
        if req.url.path.endswith('/telemetry/latest'):
            attempts+=1
            if attempts==1:raise httpx.ConnectError('fixture outage')
        return normal(req)
    replies=[call('platform__get_telemetry',{'device_id':30}),{'role':'assistant','content':'{"summary":"读取完成"}'}]
    async def provider(*args):return replies.pop(0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await Worker(cfg,store,Queue(),client,{t.task_id:{'jwt':'token-91001'}},provider).run_task(row)
    report=await store.report(t.task_id,91001)
    assert report['status']=='succeeded' and len(report['calls'])==2 and len(report['steps'])==1
    assert [c['status'] for c in report['tool_calls']]==['error','ok']


@pytest.mark.asyncio
async def test_real_redis_duplicate_stream_database_claim(env):
    from redis.asyncio import Redis
    cfg,store=env;cfg.redis_prefix='ma:m1-regression:'+uuid4().hex+':'
    queue=Redis.from_url(cfg.redis_url,decode_responses=True);stream=cfg.redis_prefix+'tasks'
    t=task();await store.create(t)
    async def provider(*args):return {'role':'assistant','content':'{"clarification":"请选择设备"}'}
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend(cfg,[]))) as client:
        worker=Worker(cfg,store,queue,client,{t.task_id:{'jwt':'token-91001'}},provider)
        try:
            await queue.xgroup_create(stream,worker.group,id='0',mkstream=True)
            await worker.delivery()
            await queue.xadd(stream,{'protocol':'ma/v1','task_id':str(t.task_id)})
            messages=await queue.xreadgroup(worker.group,worker.id,{stream:'>'},count=20)
            await worker.consume(messages);await asyncio.gather(*list(worker.jobs.values()))
            await worker.consume(messages)
            assert (await store.report(t.task_id,91001))['model_calls']==1
            assert (await queue.xpending(stream,worker.group))['pending']==0
        finally:await queue.delete(stream);await queue.aclose()


@pytest.mark.asyncio
async def test_model_tool_deadline_budgets_are_enforced(env):
    cfg,store=env;cfg.max_rounds=2
    async def provider(*args):return call('platform__get_telemetry',{'device_id':30})
    report,_=await execute(env,provider=provider)
    assert report['error']['code']=='model_budget_exceeded' and report['model_calls']==2
    cfg.max_rounds=8;cfg.max_calls=1
    report,_=await execute(env,body=task(user=91002),provider=provider)
    assert report['error']['code']=='tool_budget_exceeded' and len(report['calls'])==1
    async def slow(*args):await asyncio.sleep(.2)
    cfg.model_timeout=.01
    report,_=await execute(env,body=task(user=91003),provider=slow)
    assert report['status']=='timeout' and not report['calls']
    t=task(user=91004);await store.create(t);r=await store.claim(t.task_id,'fixture')
    await store.pool.execute("UPDATE ma_tasks SET deadline=now()-interval '1 second' WHERE task_id=$1",t.task_id)
    with pytest.raises(TimeoutError):await store.check(t.task_id,r['execution_epoch'])


@pytest.mark.asyncio
async def test_signed_create_replay_payload_tampering_and_result_schema(env):
    from ops_agent.schemas import ResultEnvelope
    cfg,store=env;app=create_app(cfg,store,Queue(),run_worker=False)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        t=task();raw=t.model_dump(mode='json');raw['created_at']=datetime.now(timezone.utc).isoformat()
        async def identity(jwt):return {'user_id':91001,'role':'operator'}
        app.state.worker.executor.identity=identity
        actor=claims({'user_id':91001,'role':'operator'},t.session_id,t.trace_id,hashlib.sha256(t.user_request.encode()).hexdigest())
        headers={'X-PowerSystem-Token':cfg.service_token,'X-MA-Actor':sign(actor,cfg.actor_key),'Authorization':'Bearer fixture'}
        headers['X-MA-Envelope']=sign({'aud':'ma-envelope','kid':'ma-v1','iat':int(time.time()),'exp':int(time.time())+450,'hash':digest(raw)},cfg.envelope_key)
        reply=await client.post('/internal/ma/v1/tasks',headers=headers,json=raw)
        assert reply.status_code==202
        ResultEnvelope.model_validate(reply.json())
        assert (await client.post('/internal/ma/v1/tasks',headers=headers,json=raw)).json()['task_id']==str(t.task_id)
        assert (await client.post('/internal/ma/v1/tasks',headers=headers,json={**raw,'intent':'tampered'})).status_code==403
        assert (await client.post('/internal/ma/v1/tasks',headers=headers,json={**raw,'protocol':'ma/v2'})).status_code==400
        with pytest.raises(Exception):ResultEnvelope.model_validate({**reply.json(),'unknown':'bad'})


def test_history_preserves_platform_24_hour_contract():
    from ops_agent.platform import Telemetry
    valid={"device_id":2,"mode":"history","metric":"temperature","start":"2026-10-09T00:00:00+08:00","end":"2026-10-10T00:00:00+08:00"}
    assert Telemetry.model_validate(valid).mode=="history"
    for override in ({"end":"2026-10-12T00:00:00+08:00"},{"metric":None},{"start":"2026-10-09T00:00:00"},{"end":valid["start"]}):
        with pytest.raises(ValueError): Telemetry.model_validate({**valid,**override})


@pytest.mark.asyncio
async def test_strict_business_output_degrades_to_facts_without_leaking_prose(env):
    replies=[call("platform__get_device",{"device_id":30}),{"role":"assistant","content":'{"summary":"ok","recommendations":[{"secret":"never-export-fixture-secret"}]}'}]
    async def provider(messages,schemas):return replies.pop(0)
    report,_=await execute(env,provider=provider)
    assert report["status"]=="succeeded" and len(report["calls"])==1
    assert report['business_quality']['render_state']=='degraded'
    assert report['business_quality']['facts'] and '回答已降级为程序事实' in report['diagnosis']['summary']
    from ops_agent.business_quality import valid_quality
    assert valid_quality(report['business_quality'])
    assert 'never-export-fixture-secret' not in json.dumps(report,ensure_ascii=False)


@pytest.mark.asyncio
async def test_session_delete_and_admission_real_transaction_race(env):
    _,store=env
    for index in range(12):
        t=task(user=91100+index);operation=uuid4()
        create=store.create(t);guard=store.guard(t.session_id,t.actor.user_id,operation,'reserve')
        actions=[create,guard] if index%2 else [guard,create]
        outcomes=await asyncio.gather(*actions,return_exceptions=True)
        failures=[r for r in outcomes if isinstance(r,Exception)]
        assert len(failures)==1 and failures[0].status_code==409
        exists=await store.pool.fetchval('SELECT EXISTS(SELECT 1 FROM ma_tasks WHERE task_id=$1)',t.task_id)
        deleting=await store.pool.fetchval('SELECT EXISTS(SELECT 1 FROM ma_session_guards WHERE session_id=$1)',t.session_id)
        assert exists != deleting
        if exists:await store.cancel(t.task_id,t.actor.user_id)
        else:
            await store.guard(t.session_id,t.actor.user_id,operation,'release')
            assert (await store.create(t))[1]
            await store.cancel(t.task_id,t.actor.user_id)


@pytest.mark.asyncio
async def test_tool_timeout_has_exactly_two_persisted_attempts(env):
    cfg,store=env;cfg.tool_timeout=.01;t=task();await store.create(t);row=await store.claim(t.task_id,'fixture');normal=backend(cfg,[])
    async def slow_backend(req):
        if req.url.path.endswith('/telemetry/latest'):await asyncio.sleep(.1)
        return normal(req)
    async def provider(*args):return call('platform__get_telemetry',{'device_id':30})
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_backend)) as client:
        await Worker(cfg,store,Queue(),client,{t.task_id:{'jwt':'token-91001'}},provider).run_task(row)
    report=await store.report(t.task_id,91001)
    assert report['status']=='failed' and len(report['calls'])==2
    assert all(c['status']=='failed' and c['error']['retryable'] for c in report['calls'])


@pytest.mark.asyncio
async def test_result_save_failure_stops_remaining_steps(env):
    cfg,store=env;t=task();await store.create(t);row=await store.claim(t.task_id,'fixture');paths=[];model_count=0
    async def fail_save(*args):raise OSError('synthetic database result-save failure')
    original=store.end_call;store.end_call=fail_save
    async def provider(*args):
        nonlocal model_count;model_count+=1
        return {'role':'assistant','content':None,'tool_calls':[call('platform__get_device',{'device_id':30},'a')['tool_calls'][0],call('platform__get_prediction',{'device_id':30},'b')['tool_calls'][0]]}
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend(cfg,paths))) as client:
            await Worker(cfg,store,Queue(),client,{t.task_id:{'jwt':'token-91001'}},provider).run_task(row)
    finally:store.end_call=original
    report=await store.report(t.task_id,91001)
    assert report['status']=='failed' and model_count==1
    assert not any(path.endswith('/prediction') for _,path in paths)
    assert len(report['calls'])==1 and report['calls'][0]['status']=='failed'


@pytest.mark.asyncio
async def test_stream_rejects_bad_protocol_extra_fields_and_orphan_ids(env):
    cfg,store=env;t=task();await store.create(t)
    async def forbidden(*args):pytest.fail('malformed stream must never run a model')
    worker=Worker(cfg,store,Queue(),None,{},forbidden)
    payloads=[{'protocol':'ma/v2','task_id':str(t.task_id)},{'protocol':'ma/v1','task_id':str(t.task_id),'jwt':'forged'},{'protocol':'ma/v1','task_id':'bad'},{'protocol':'ma/v1','task_id':str(uuid4())}]
    await worker.consume([('fixture-stream',[(str(index),value) for index,value in enumerate(payloads)])])
    assert not worker.jobs and (await store.get(t.task_id))['status']=='queued'


@pytest.mark.asyncio
async def test_MA_C20_task_record_soft_delete_keeps_audit(env):
    _,store=env;t=task();await store.create(t)
    with pytest.raises(Exception):await store.soft_delete(t.task_id,91001)
    with pytest.raises(Exception):await store.soft_delete(t.task_id,91002)
    await store.cancel(t.task_id,91001)
    first=await store.soft_delete(t.task_id,91001);second=await store.soft_delete(t.task_id,91001)
    assert first['deleted'] and not first['already_deleted'] and second['already_deleted']
    assert second['deleted_at']==first['deleted_at']
    assert all(r['task_id']!=t.task_id for r in (await store.listing(91001))['list'])
    row=await store.get(t.task_id,91001)
    assert row['deleted_at'] is not None and row['deleted_by']==91001
    assert (await store.report(t.task_id,91001))['status']=='cancelled'
    assert await store.pool.fetchval("SELECT count(*) FROM ma_task_events WHERE task_id=$1 AND reason='user_deleted'",t.task_id)==1


@pytest.mark.asyncio
async def test_MA_C21_task_delete_route_ownership_and_status(env):
    cfg,store=env;app=create_app(cfg,store,Queue(),run_worker=False)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        t=task();await store.create(t)
        def headers(user):return {"X-PowerSystem-Token":cfg.service_token,"X-MA-Actor":sign(claims({"user_id":user,"role":"operator"},t.session_id,t.trace_id,""),cfg.actor_key)}
        url="/internal/ma/v1/tasks/"+str(t.task_id)
        assert (await client.delete(url,headers=headers(91001))).status_code==409
        assert (await client.delete(url,headers=headers(91002))).status_code==404
        assert (await client.delete(url,headers={**headers(91001),"X-MA-Actor":"forged"})).status_code==403
        await store.cancel(t.task_id,91001)
        assert (await client.delete(url,headers=headers(91001))).status_code==200
        assert (await client.get(url,headers=headers(91001))).status_code==200
