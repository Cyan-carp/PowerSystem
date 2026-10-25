import asyncio
import json
import os
from uuid import UUID,uuid4
from datetime import datetime,timezone
import httpx
import pytest
import pytest_asyncio
from rag_agent.store import Store
from rag_agent.config import Config
from rag_agent.main import create_app
from rag_agent.run_store import RunStore,unpack,LostLease
from rag_agent.run_worker import AuthVault,Runtime
from rag_agent.tools import Tools


class Provider:
    def __init__(self,delay=0):self.calls=0;self.delay=delay;self.seen=[]
    async def complete(self,messages,schemas=None):
        self.calls+=1;self.seen.append(messages)
        await asyncio.sleep(self.delay)
        return dict(role='assistant',content='这是本轮解释：'+messages[-1]['content'])


@pytest_asyncio.fixture
async def env():
    url=os.environ.get('RAG_RUN_TEST_URL','')
    if not url:pytest.skip('independent S03 PostgreSQL acceptance database required')
    assert url.startswith('postgresql://s03_test@127.0.0.1:25433/') and url.endswith('_test'),'S03 dedicated database required'
    cfg=Config(model='deepseek-flash',llm_url='https://api.deepseek.com',llm_key='',service_token='s03-synthetic-service-token-00000000',database_url=url,redis_url='redis://127.0.0.1:1',v4_assistant_enabled=True,v4_runs_enabled=True,model_thinking='disabled')
    store=await Store.open(url)
    # Dedicated S03 synthetic test DB only, serial test fixture reset.
    await store.pool.execute('TRUNCATE rag_sessions CASCADE')
    user=1000000000+int(uuid4().hex[:7],16)
    provider=Provider()
    app=create_app(cfg,store,object(),provider,Tools(cfg,None,None),run_worker=False)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        yield cfg,store,user,provider,app,client
    await store.pool.close()


def headers(cfg,key=None):return {'X-PowerSystem-Token':cfg.service_token,'Authorization':'Bearer s03-synthetic-user-token','Idempotency-Key':str(key or uuid4())}


async def accept(env,**kw):
    cfg,store,user,p,app,client=env
    return await client.post('/internal/agent/runs',headers=headers(cfg,kw.pop('key',None)),json=dict(user_id=kw.pop('user',user),message=kw.pop('message','解释TCP'),**kw))


async def finish_one(env):
    app=env[4]
    row=await app.state.runs.claim(app.state.run_worker.id)
    assert row
    await app.state.run_worker.execute(row)
    return row


@pytest.mark.asyncio
async def test_transaction_admission_and_context_frozen(env):
    cfg,store,user,p,app,c=env
    a=(await accept(env)).json();b=(await accept(env)).json()
    assert a['session_id']!=b['session_id']
    before=(await accept(env,session_id=a['session_id'])).json()
    row=unpack(await store.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1',UUID(before['run_id'])))
    assert row['turn_id'] and row['context_snapshot']['turns'][0]['response']['status']=='queued'
    assert await store.pool.fetchval('SELECT run_id FROM rag_turns WHERE id=$1',row['turn_id'])==row['id']
    assert await store.pool.fetchval('SELECT count(*) FROM assistant_run_outbox WHERE run_id=$1',row['id'])==1
    assert not AuthVault(cfg.service_token).open(user,row['auth_sealed'])['jwt'] in str(dict(row))
    await finish_one(env)
    fixed=json.loads(await store.pool.fetchval('SELECT context_snapshot FROM assistant_runs WHERE id=$1',row['id']))
    assert fixed['turns'][0]['response']['status']=='queued'
    assert (await accept(env,session_id=a['session_id'],user=user+1)).status_code==404


@pytest.mark.asyncio
async def test_concurrent_idempotency_and_lost_receipt(env):
    cfg,store,user,p,app,c=env
    key=uuid4()
    responses=await asyncio.gather(*(accept(env,key=key) for _ in range(16)))
    assert {r.status_code for r in responses}=={202}
    assert len({r.json()['run_id'] for r in responses})==1
    assert (await accept(env,key=key,message='不同内容')).status_code==409
    original=responses[0].json()
    assert (await accept(env,key=key)).json()==original
    assert (await accept(env,key=key,user=user+1)).json()['run_id']!=original['run_id']
    count=await store.pool.fetchval('SELECT count(*) FROM assistant_runs WHERE user_id=$1',user)
    assert count==1
    row=await finish_one(env)
    repeated=await accept(env,key=key)
    assert repeated.json()['run_id']==original['run_id'] and p.calls==1


@pytest.mark.asyncio
async def test_snapshot_events_terminal_and_owner(env):
    cfg,store,user,p,app,c=env
    a=(await accept(env)).json();rid=a['run_id']
    await finish_one(env)
    snapshot=(await c.get('/internal/agent/runs/'+rid,params={'user_id':user},headers=headers(cfg))).json()
    assert snapshot['status']=='succeeded',snapshot
    events,term=await app.state.runs.event_batch(UUID(rid),user,0)
    assert term and [e['seq'] for e in events]==list(range(1,len(events)+1))
    assert snapshot['terminal_event']==events[-1]
    assert snapshot['last_seq']==events[-1]['seq'] and snapshot['answer_version']==events[-1]['data']['answer_version']
    text=[e for e in events if e['type']=='text_delta'][-1]
    assert text['data']['text']==snapshot['final_answer']
    assert all('percent' not in e['data'] for e in events)
    for suffix in ('','/events','/cancel'):
        response=await c.request('POST' if suffix=='/cancel' else 'GET','/internal/agent/runs/'+rid+suffix,params={'user_id':user+1},headers=headers(cfg))
        assert response.status_code==404
    assert (await c.post('/internal/agent/runs/'+rid+'/cancel',params={'user_id':user},headers=headers(cfg))).json()['status']=='succeeded'
    history=(await c.get('/internal/agent/sessions/'+a['session_id'],params={'user_id':user},headers=headers(cfg))).json()
    assert history['turns'][0]['id']==a['turn_id'] and history['turns'][0]['run_id']==rid


@pytest.mark.asyncio
async def test_cursors_expiration_and_snapshot_resume(env):
    cfg,store,user,p,app,c=env
    a=(await accept(env)).json();rid=UUID(a['run_id']);await finish_one(env)
    url='/internal/agent/runs/'+str(rid)+'/events'
    params={'user_id':user,'after_seq':0}
    assert (await c.get(url,params=params,headers={**headers(cfg),'Last-Event-ID':'1'})).status_code==400
    assert (await c.get(url,params={**params,'after_seq':99999},headers=headers(cfg))).status_code==400
    for bad in ('-1','1.2','9223372036854775808','abc'):
        assert (await c.get(url,params={**params,'after_seq':bad},headers=headers(cfg))).status_code==400
    all_events=(await c.get(url,params=params,headers=headers(cfg))).text
    replay=(await c.get(url,params=params,headers=headers(cfg))).text
    # Replayed subscriptions expose stable seqs, allowing exact client deduplication.
    assert all_events==replay
    assert 's03-synthetic-user-token' not in all_events
    await store.pool.execute("UPDATE assistant_run_events SET at=now()-interval '8 days' WHERE run_id=$1",rid)
    assert (await c.get(url,params=params,headers=headers(cfg))).status_code==410
    snap=await app.state.runs.snapshot(rid,user)
    resumed=await c.get(url,params={**params,'after_seq':snap['last_seq']},headers=headers(cfg))
    assert resumed.status_code==200 and resumed.text=='' and snap['final_answer']


@pytest.mark.asyncio
async def test_queue_cancel_budget_and_fencing(env):
    cfg,store,user,p,app,c=env
    a=(await accept(env)).json();rid=UUID(a['run_id'])
    first=await app.state.runs.cancel(rid,user);again=await app.state.runs.cancel(rid,user)
    assert first==again and first['status']=='cancelled'
    b=(await accept(env)).json();bid=UUID(b['run_id'])
    await store.pool.execute("UPDATE assistant_runs SET deadline=now()-interval '1 second' WHERE id=$1",bid)
    await finish_one(env)
    assert (await app.state.runs.snapshot(bid,user))['error']['code']=='deadline_exceeded'
    d=(await accept(env)).json();did=UUID(d['run_id'])
    worker1,worker2=uuid4(),uuid4()
    old=await app.state.runs.claim(worker1)
    await store.pool.execute("UPDATE assistant_runs SET lease_until=now()-interval '1 second' WHERE id=$1",did)
    new=await app.state.runs.claim(worker2)
    assert new['lease_epoch']==old['lease_epoch']+1
    with pytest.raises(LostLease):await app.state.runs.emit(did,worker1,old['lease_epoch'],'progress',{})


@pytest.mark.asyncio
async def test_running_cancel_does_not_cancel_on_disconnect(env):
    cfg,store,user,p,app,c=env
    p.delay=.5
    a=(await accept(env)).json();rid=UUID(a['run_id'])
    row=await app.state.runs.claim(app.state.run_worker.id)
    work=asyncio.create_task(app.state.run_worker.execute(row))
    await asyncio.sleep(.1)
    # No live subscriber exists, yet the worker keeps running.
    assert not work.done()
    cancel=await app.state.runs.cancel(rid,user)
    assert cancel['cancel_requested']
    await work
    assert (await app.state.runs.snapshot(rid,user))['status']=='cancelled'


@pytest.mark.asyncio
async def test_model_journal_recovery_and_two_run_limit(env):
    cfg,store,user,p,app,c=env
    entries=[(await accept(env)).json() for _ in range(3)]
    w=app.state.run_worker.id
    first=await app.state.runs.claim(w);second=await app.state.runs.claim(w)
    # Other tests can leave rows behind, restrict this dedicated owner explicitly.
    own=await store.pool.fetchval("SELECT count(*) FROM assistant_runs WHERE user_id=$1 AND status='running' AND lease_until>now()",user)
    assert own==2
    third=await app.state.runs.claim(w)
    assert third is None or third['user_id']!=user
    runtime=Runtime(app.state.runs,first,cfg,w,type('Ops',(),{})())
    result={}
    out=await runtime.model(0,p,[{'role':'user','content':'固定调用'}],None,result)
    out2=await runtime.model(0,p,[{'role':'user','content':'固定调用'}],None,result)
    assert out==out2 and p.calls==1


@pytest.mark.asyncio
async def test_migration_repeat_old_rows_and_soft_delete(env):
    cfg,store,user,p,app,c=env
    sid=uuid4();await store.ensure(sid,user,'旧会话',create=True)
    turn=await store.save(sid,'旧问题',{'status':'answered','answer_markdown':'旧回答'},[])
    reopened=await Store.open(cfg.database_url)
    assert (await reopened.turns(sid))[0]['run_id'] is None
    await reopened.pool.close()
    a=(await accept(env,session_id=str(sid))).json()
    assert await store.delete(sid,user)
    assert not await store.ensure(sid,user,'')
    assert (await app.state.runs.snapshot(UUID(a['run_id']),user))['turn_id']!=turn


@pytest.mark.asyncio
async def test_input_disabled_and_no_plain_auth_leak(env):
    cfg,store,user,p,app,c=env
    assert (await c.post('/internal/agent/runs',json={'message':'x','user_id':user})).status_code==401
    assert (await c.post('/internal/agent/runs',headers={**headers(cfg),'Idempotency-Key':'bad'},json={'message':'x','user_id':user})).status_code==400
    assert (await accept(env,message='  ')).status_code==400
    cfg.v4_runs_enabled=False
    assert (await accept(env)).status_code==503


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['pending','effects','no_effect'])
async def test_unknown_write_cancel_uses_isolated_authority_ledger(env,outcome):
    cfg,store,user,p,app,c=env
    a=(await accept(env)).json();rid=UUID(a['run_id']);action=uuid4()
    # Isolated controller ledger only: no executor, no business write route.
    await store.pool.execute('CREATE TABLE IF NOT EXISTS s03_fixture_authority(action_id uuid primary key,state text,cancel_count integer default 0,execution_count integer default 1)')
    await store.pool.execute('INSERT INTO s03_fixture_authority(action_id,state) VALUES($1,$2)',action,outcome)
    class Authority:
        async def inspect(self,row,cancel=False):
            if cancel:await store.pool.execute('UPDATE s03_fixture_authority SET cancel_count=cancel_count+1 WHERE action_id=$1',action)
            state=await store.pool.fetchval('SELECT state FROM s03_fixture_authority WHERE action_id=$1',action)
            return dict(pending=state=='pending',effects=state=='effects',actions=[dict(action_id=str(action),status=state,stale=False,observed_at=datetime.now(timezone.utc).isoformat())])
    app.state.run_worker.authority=Authority()
    projection=dict(action_id=str(action),task_id=str(uuid4()),approval_id=str(uuid4()),status='execution_uncertain',authority_revision=1,observed_at=datetime.now(timezone.utc).isoformat(),raw_report='must not persist',credential='must not persist')
    await app.state.runs.project_authority(rid,user,[projection])
    await app.state.runs.project_authority(rid,user,[projection])
    assert (await app.state.runs.snapshot(rid,user))['status']=='reconciling'
    assert 'must not persist' not in str(await app.state.runs.snapshot(rid,user))
    cancel=await c.post('/internal/agent/runs/'+str(rid)+'/cancel',params={'user_id':user},headers=headers(cfg))
    assert cancel.status_code==(202 if outcome=='pending' else 200)
    if outcome=='pending':
        assert cancel.json()['status']=='pending_reconciliation'
        await store.pool.execute("UPDATE assistant_runs SET reconciliation_deadline=now()-interval '1 second',deadline=now()-interval '1 second' WHERE id=$1",rid)
    await finish_one(env)
    result=await app.state.runs.snapshot(rid,user)
    assert result['status']==('cancelled' if outcome=='no_effect' else 'partial')
    assert result['requires_manual_reconciliation']==(outcome=='pending')
    assert await store.pool.fetchval('SELECT execution_count FROM s03_fixture_authority WHERE action_id=$1',action)==1
    assert p.calls==0


@pytest.mark.asyncio
async def test_running_budget_exhaustion_and_context_ownership(env):
    cfg,store,user,p,app,c=env
    p.delay=.8;cfg.total_timeout=.2
    a=(await accept(env)).json();rid=UUID(a['run_id'])
    await finish_one(env)
    snap=await app.state.runs.snapshot(rid,user)
    assert snap['status']=='failed' and snap['error']['code']=='deadline_exceeded',snap


@pytest.mark.asyncio
async def test_multi_turn_out_of_order_completion_stays_at_original_turn(env):
    cfg,store,user,p,app,c=env
    a=(await accept(env,message='解释TCP')).json()
    b=(await accept(env,message='解释UDP',session_id=a['session_id'])).json()
    rowa=await app.state.runs.claim(app.state.run_worker.id);rowb=await app.state.runs.claim(app.state.run_worker.id)
    await app.state.run_worker.execute(rowb)
    await app.state.run_worker.execute(rowa)
    history=await store.turns(UUID(a['session_id']))
    assert [t['id'] for t in history]==[a['turn_id'],b['turn_id']]
    assert 'TCP' in history[0]['response']['answer_markdown'] and 'UDP' in history[1]['response']['answer_markdown']
    # B sees A's accepted question and queued status, never A's later completed answer.
    assert 'queued' in str(p.seen[0]) and '这是本轮解释：解释TCP' not in str(p.seen[0])


@pytest.mark.asyncio
async def test_unknown_action_arriving_during_model_cannot_be_cancelled_as_read_only(env):
    cfg,store,user,p,app,c=env
    p.delay=.8
    a=(await accept(env)).json();rid=UUID(a['run_id'])
    row=await app.state.runs.claim(app.state.run_worker.id)
    work=asyncio.create_task(app.state.run_worker.execute(row));await asyncio.sleep(.1)
    class Authority:
        async def inspect(self,row,cancel=False):return dict(pending=True,effects=False,actions=row['actions'])
    app.state.run_worker.authority=Authority()
    projection=dict(action_id=str(uuid4()),task_id=str(uuid4()),approval_id=str(uuid4()),status='execution_uncertain',authority_revision=1,observed_at=datetime.now(timezone.utc).isoformat())
    await app.state.runs.project_authority(rid,user,[projection])
    await store.pool.execute("UPDATE assistant_runs SET reconciliation_deadline=now()-interval '1 second' WHERE id=$1",rid)
    await app.state.runs.cancel(rid,user);await work
    result=await app.state.runs.snapshot(rid,user)
    assert result['status']=='partial' and result['requires_manual_reconciliation']


@pytest.mark.asyncio
async def test_more_than_200_events_streams_through_terminal(env):
    cfg,store,user,p,app,c=env
    a=(await accept(env)).json();rid=UUID(a['run_id'])
    row=await app.state.runs.claim(app.state.run_worker.id)
    for n in range(205):await app.state.runs.emit(rid,app.state.run_worker.id,row['lease_epoch'],'progress',dict(phase='checking',message='检查中'))
    await app.state.run_worker.execute(row)
    snap=await app.state.runs.snapshot(rid,user)
    response=await c.get('/internal/agent/runs/'+str(rid)+'/events',params={'user_id':user},headers=headers(cfg))
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert len(events)==snap['last_seq'] and events[-1]==snap['terminal_event']


@pytest.mark.asyncio
async def test_egress_failure_is_failed_and_profile_change_never_replays(env):
    cfg,store,user,p,app,c=env
    class Blocked:
        schemas=[]
        async def check_egress(self):raise ValueError('synthetic unavailable')
    app.state.tools.run_tools=lambda ops:Blocked()
    a=(await accept(env)).json();await finish_one(env)
    snap=await app.state.runs.snapshot(UUID(a['run_id']),user)
    assert snap['status']=='failed' and snap['error']['code']=='egress_unavailable' and p.calls==0
    b=(await accept(env)).json();cfg.model='different-model'
    await finish_one(env)
    assert (await app.state.runs.snapshot(UUID(b['run_id']),user))['error']['code']=='model_configuration_changed'


@pytest.mark.asyncio
async def test_worker_loop_survives_transient_postgres_error(env):
    import asyncpg
    cfg,store,user,p,app,c=env
    worker=app.state.run_worker;original=app.state.runs.claim;attempts=0
    async def claim(*args):
        nonlocal attempts
        attempts+=1
        if attempts==1:raise asyncpg.PostgresConnectionError('synthetic disconnect')
        return await original(*args)
    app.state.runs.claim=claim
    a=(await accept(env)).json()
    loop=asyncio.create_task(worker.loop())
    try:
        for _ in range(40):
            if (await app.state.runs.snapshot(UUID(a['run_id']),user))['status']=='succeeded':break
            await asyncio.sleep(.1)
        else:raise AssertionError('worker did not recover')
        assert attempts>1 and not loop.done()
    finally:loop.cancel();await asyncio.gather(loop,return_exceptions=True);await worker.stop()


@pytest.mark.asyncio
async def test_delayed_child_is_tracked_until_actual_completion(env):
    cfg,store,user,p,app,c=env
    tid=str(uuid4());gets=0
    class SlowTools:
        schemas=[]
        async def execute(self,name,args,jwt,result):
            task=dict(task_id=tid,status='queued' if name=='dispatch_ops_task' else 'succeeded',tool_calls=[],sources=[])
            result['task_cards']=[task]
            return task
    def tools(ops):
        async def get(task):
            nonlocal gets
            gets+=1
            return dict(task_id=tid,status='succeeded' if gets>=2 else 'executing',tool_calls=[],sources=[])
        ops.get=get
        return SlowTools()
    app.state.tools.run_tools=tools
    async def model(messages,schemas=None):
        if not any(m['role']=='tool' for m in messages):return dict(role='assistant',content='',tool_calls=[dict(id='child',function=dict(name='dispatch_ops_task',arguments='{"intent":"合成读取"}'))])
        return dict(role='assistant',content='合成读取已完成')
    p.complete=model
    a=(await accept(env,message='执行合成读取')).json();await finish_one(env)
    snap=await app.state.runs.snapshot(UUID(a['run_id']),user)
    assert snap['status']=='succeeded' and gets>=2 and snap['result_refs'][0]['status']=='succeeded'


@pytest.mark.asyncio
async def test_checkpoint_redaction_and_durable_child_cancel_outbox(env):
    cfg,store,user,p,app,c=env
    async def secret_model(messages,schemas=None):return dict(role='assistant',content=cfg.service_token)
    p.complete=secret_model
    first=(await accept(env)).json();await finish_one(env)
    checkpoints=await store.pool.fetch('SELECT checkpoint FROM assistant_checkpoints WHERE run_id=$1',UUID(first['run_id']))
    assert checkpoints and all(cfg.service_token.encode() not in r['checkpoint'] for r in checkpoints)
    a=(await accept(env)).json();rid=UUID(a['run_id']);row=await app.state.runs.claim(app.state.run_worker.id)
    await app.state.runs.child(rid,app.state.run_worker.id,row['lease_epoch'],uuid4(),'queued')
    await app.state.runs.enqueue_child_cancel(rid)
    await app.state.runs.finish(rid,app.state.run_worker.id,row['lease_epoch'],'cancelled',{},[])
    assert await store.pool.fetchval('SELECT octet_length(auth_sealed)>0 FROM assistant_runs WHERE id=$1',rid)
    requests=[]
    class Ops:
        async def request(self,*args):requests.append(args);return {'status':'cancelled'}
    app.state.run_worker.bound_ops=lambda *args:Ops()
    await app.state.run_worker.drain_child_cancels()
    assert len(requests)==1
    assert not await store.pool.fetchval('SELECT cancel_children FROM assistant_run_outbox WHERE run_id=$1',rid)
    assert await store.pool.fetchval('SELECT octet_length(auth_sealed) FROM assistant_runs WHERE id=$1',rid)==0


@pytest.mark.asyncio
async def test_cancel_race_partial_and_terminal_authority_unavailable_preserve_status(env):
    cfg,store,user,p,app,c=env
    a=(await accept(env)).json();rid=UUID(a['run_id']);row=await app.state.runs.claim(app.state.run_worker.id)
    await app.state.runs.cancel(rid,user)
    await app.state.runs.finish(rid,app.state.run_worker.id,row['lease_epoch'],'partial',{'answer_markdown':'已有结果'},[],{'code':'objective_incomplete'})
    assert (await app.state.runs.snapshot(rid,user))['status']=='cancelled'
    b=(await accept(env)).json();bid=UUID(b['run_id']);brow=await app.state.runs.claim(app.state.run_worker.id)
    projection=dict(action_id=str(uuid4()),task_id=str(uuid4()),approval_id=str(uuid4()),status='succeeded',authority_revision=2,observed_at=datetime.now(timezone.utc).isoformat(),stale=False)
    await app.state.runs.project_authority(bid,user,[projection])
    await app.state.runs.finish(bid,app.state.run_worker.id,brow['lease_epoch'],'partial',{'answer_markdown':'已核实副作用'},[])
    class Unavailable:
        async def inspect(self,*args,**kw):raise OSError('synthetic unavailable')
    app.state.run_worker.authority=Unavailable()
    response=await c.post('/internal/agent/runs/'+str(bid)+'/cancel',params={'user_id':user},headers=headers(cfg))
    assert response.status_code==202 and response.json()['status']=='partial' and response.json()['authority_sync_status']=='pending_reconciliation'
    from rag_agent.run_store import AwaitingAuthority
    pending=(await accept(env)).json();pid=UUID(pending['run_id']);prow=await app.state.runs.claim(app.state.run_worker.id)
    await app.state.runs.project_authority(pid,user,[{**projection,'action_id':str(uuid4()),'status':'pending'}])
    with pytest.raises(AwaitingAuthority):await app.state.runs.finish(pid,app.state.run_worker.id,prow['lease_epoch'],'succeeded',{'answer_markdown':'不能提前成功'},[])
    assert (await app.state.runs.snapshot(pid,user))['status']=='awaiting_approval'
