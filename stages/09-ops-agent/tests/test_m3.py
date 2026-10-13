"""MA-C26..31 contracts, real PostgreSQL races and fault recovery."""
import asyncio
import copy
import json
import os
import time
from datetime import datetime,timezone
from uuid import uuid4
import pytest
import pytest_asyncio
from fastapi import HTTPException
from ops_agent.config import Config
from ops_agent.store import Store
from ops_agent.schemas import Task,ApprovalDecision
from ops_contract import digest,validate_catalog
from ops_write_contract import sign,verify,proof,matches,explicit_restart,TOOL,restart_args


def registry():
    def asset(aid,kind,parent,caps):
        return dict(asset_id=aid,kind=kind,display_name=aid,project_scope='powersystem',parent_id=parent,
                    enabled=True,criticality='drill',environment='fixture',version=1,
                    verified_at='2026-10-12T00:00:00+00:00',connection_ref='local-drill',identity_hash='a'*64,capabilities=caps)
    return [asset('fixture-drill-host','host',None,[]),asset('fixture-drill','container','fixture-drill-host',[TOOL,'docker.inspect_container'])]


def snapshot(assets=None,policy=None):
    return dict(asset_id='fixture-drill',asset_version=1,registry_hash=digest(assets or registry()),container_id='b'*64,
       image_digest='sha256:'+'c'*64,config_hash='d'*64,policy_version=digest(policy or {'version':'fixture-m3-v1'}),
       state='running',running=True,health='healthy',started_at='2026-10-12T00:00:00+00:00',stateless_verified=True,
       collected_at=datetime.now(timezone.utc).isoformat())


def grant(row=None,a=None,cid=None):
    s=snapshot();now=int(time.time())
    return dict(aud='ma-drill-executor',kid='ma-write-v1',iat=now,exp=now+30,action='restart',
         task_id=str(row['task_id'] if row else uuid4()),trace_id=str(row['trace_id'] if row else uuid4()),
         execution_epoch=row['execution_epoch'] if row else 1,step_no=a['step_no'] if a else 1,call_id=str(cid or uuid4()),
         user_id=91001,role='admin',args_hash=digest({'asset_id':'fixture-drill'}),
         deadline=row['deadline'].timestamp() if row else time.time()+300,approval_id=str(a['approval_id'] if a else uuid4()),
         **{k:s[k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version')})


@pytest.mark.parametrize('change',[{'role':'operator'},{'exp':0},{'exp':int(time.time())+120},
    {'container_id':'short'},{'asset_id':'$(id)'},{'args_hash':'x'},{'extra':'shell'},{'execution_epoch':True},
    {'deadline':float('nan')},{'approval_id':'bad'},{'action':'exec'},{'image_digest':'latest'}])
def test_c26_write_signature_rejects_invalid_context(change):
    value={**grant(),**change}
    with pytest.raises(ValueError): verify(sign(value,'k'*32),'k'*32)


def test_c26_tampering_and_distinct_key():
    token=sign(grant(),'k'*32)
    assert verify(token,'k'*32)['role']=='admin'
    with pytest.raises(ValueError):verify(token,'r'*32)
    with pytest.raises(ValueError):verify(token[:-1]+('a' if token[-1]!='a' else 'b'),'k'*32)


@pytest.mark.parametrize('text',['服务器慢，帮我处理','如何重启容器','不要重启','如果重启会怎样','解释restart','do not restart'])
def test_c26_diagnostic_or_explanatory_intent_is_not_write(text):assert not explicit_restart(text)


def test_c26_only_drill_capability_and_strict_args():
    assets=registry();validate_catalog(assets)
    assets[1]['criticality']='core'
    with pytest.raises(ValueError):validate_catalog(assets)
    with pytest.raises(ValueError):restart_args({'asset_id':'fixture-drill','timeout':10})
    with pytest.raises(ValueError):ApprovalDecision.model_validate({'decision':'approve','role':'admin'})


@pytest_asyncio.fixture
async def database(tmp_path):
    if not os.getenv('OPS_DATABASE_URL'):pytest.skip('dedicated MA-M3 regression database required')
    cfg=Config.load()
    if not cfg.database_url.split('?')[0].endswith('/ma_m3_ops_regression_test'):raise RuntimeError('m3_regression_database_required')
    cfg.enabled=cfg.infra_enabled=cfg.write_enabled=True;cfg.audit_dir=tmp_path
    store=await Store.open(cfg)
    await store.pool.execute('TRUNCATE ma_execution_attempts,ma_approvals,ma_policy_versions,ma_tool_calls,ma_task_steps,ma_task_events,ma_outbox,ma_tasks,ma_session_guards,ma_asset_versions,ma_assets')
    assets=registry();await store.import_assets(assets,digest(assets),91001)
    yield cfg,store
    await store.pool.close()


async def pending(database):
    cfg,store=database
    t=Task(protocol='ma/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),
           actor={'user_id':91001,'role':'admin'},intent='重启演练容器',user_request='请重启本机隔离演练容器',
           operation='restart_drill_container',constraints={'max_risk_class':'R2'},created_at=datetime.now(timezone.utc))
    tid,_=await store.create(t);row=await store.claim(tid,'m3-fixture')
    aid=await store.prepare_approval(tid,row['execution_epoch'],1,{'asset_id':'fixture-drill'},snapshot(),{'version':'fixture-m3-v1'},{'tool_calls':[],'sources':[],'warnings':[]})
    return tid,aid


async def approved(database):
    cfg,store=database;tid,aid=await pending(database)
    assert await store.decide(tid,aid,91001,'approve',snapshot())
    row=await store.claim(tid,'m3-execution')
    await store.transition(tid,row['execution_epoch'],'executing')
    a=await store.approved_plan(tid);cid=await store.begin_write(tid,row['execution_epoch'],a)
    row=await store.get(tid)
    return tid,aid,row,a,cid


@pytest.mark.asyncio
async def test_c27_pending_survives_restart_and_300_second_planning_budget(database):
    cfg,store=database;tid,aid=await pending(database)
    await store.pool.execute("UPDATE ma_tasks SET deadline=now()+interval '20 minutes' WHERE task_id=$1",tid)
    await store.recover(startup=True)
    report=await store.report(tid,91001)
    assert report['status']=='awaiting_approval' and report['approvals'][0]['status']=='pending'
    assert await store.pool.fetchval("SELECT count(*) FROM ma_tasks WHERE status IN ('planning','executing')")==0
    with pytest.raises(HTTPException) as e:await store.get(tid,91002)
    assert e.value.status_code==404


@pytest.mark.asyncio
async def test_c27_reject_repeat_and_opposite_decision(database):
    _,store=database;tid,aid=await pending(database)
    assert await store.decide(tid,aid,91001,'reject')
    assert not await store.decide(tid,aid,91001,'reject')
    with pytest.raises(HTTPException) as e:await store.decide(tid,aid,91001,'approve',snapshot())
    assert e.value.status_code==409
    assert (await store.report(tid,91001))['status']=='cancelled'
    assert await store.pool.fetchval('SELECT count(*) FROM ma_execution_attempts')==0


@pytest.mark.asyncio
async def test_c27_expired_and_cancelled_approval(database):
    _,store=database;tid,aid=await pending(database)
    await store.pool.execute("UPDATE ma_approvals SET expires_at=now()-interval '1 second' WHERE approval_id=$1",aid)
    await store.recover(startup=True)
    r=await store.report(tid,91001)
    assert r['status']=='cancelled' and r['approvals'][0]['status']=='expired'
    tid,aid=await pending(database);r=await store.cancel(tid,91001)
    assert r['status']=='cancelled' and r['approvals'][0]['status']=='cancelled'


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('container_id','e'*64),('image_digest','sha256:'+'f'*64),('config_hash','e'*64),('asset_version',2),('policy_version','e'*64),('registry_hash','f'*64)])
async def test_c28_changed_snapshot_invalidates_old_approval(database,field,value):
    _,store=database;tid,aid=await pending(database);state=snapshot();state[field]=value
    with pytest.raises(HTTPException) as e:await store.decide(tid,aid,91001,'approve',state)
    assert e.value.status_code==409
    r=await store.report(tid,91001)
    assert r['status']=='failed' and r['approvals'][0]['status']=='invalidated' and not r['execution_attempts']


@pytest.mark.asyncio
async def test_c29_concurrent_approve_single_outbox_and_single_consumption(database):
    _,store=database;tid,aid=await pending(database)
    outcomes=await asyncio.gather(*(store.decide(tid,aid,91001,'approve',snapshot()) for _ in range(4)))
    assert outcomes.count(True)==1
    assert await store.pool.fetchval('SELECT count(*) FROM ma_outbox WHERE task_id=$1',tid)==2
    row=await store.claim(tid,'first');assert await store.claim(tid,'second') is None
    await store.transition(tid,row['execution_epoch'],'executing')
    a=await store.approved_plan(tid);cid=await store.begin_write(tid,row['execution_epoch'],a)
    row=await store.get(tid);g=grant(row,a,cid)
    replies=await asyncio.gather(*(store.consume_write(g) for _ in range(4)),return_exceptions=True)
    assert sum(isinstance(r,dict) for r in replies)==1
    assert all(isinstance(r,dict) or isinstance(r,HTTPException) and r.status_code==409 for r in replies)
    assert await store.pool.fetchval('SELECT count(*) FROM ma_execution_attempts WHERE task_id=$1',tid)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('switch',['write_enabled','enabled'])
async def test_c26_write_off_blocks_consume_without_attempt(database,switch):
    cfg,store=database;tid,aid,row,a,cid=await approved(database);setattr(cfg,switch,False)
    with pytest.raises(HTTPException):await store.consume_write(grant(row,a,cid))
    assert await store.pool.fetchval('SELECT count(*) FROM ma_execution_attempts')==0


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('execution_epoch',999),('args_hash','e'*64),('container_id','e'*64),('user_id',91002),('step_no',4)])
async def test_c28_c29_bound_context_rejects_before_consumption(database,field,value):
    _,store=database;tid,aid,row,a,cid=await approved(database);g=grant(row,a,cid);g[field]=value
    with pytest.raises(HTTPException):await store.consume_write(g)
    assert await store.pool.fetchval('SELECT count(*) FROM ma_execution_attempts')==0


@pytest.mark.asyncio
async def test_c30_consumed_attempt_recovers_uncertain_and_never_requeues(database):
    _,store=database;tid,aid,row,a,cid=await approved(database)
    await store.consume_write(grant(row,a,cid));await store.recover(startup=True)
    r=await store.report(tid,91001)
    assert r['status']=='failed' and r['error']['code']=='execution_uncertain'
    assert r['execution_attempts'][0]['status']=='execution_uncertain'
    assert r['approvals'][0]['consumed_at']
    assert await store.claim(tid,'retry') is None
    await store.recover(startup=True)
    assert len((await store.report(tid,91001))['execution_attempts'])==1


@pytest.mark.asyncio
async def test_c30_approval_queue_auth_loss_does_not_execute(database):
    _,store=database;tid,aid=await pending(database)
    await store.decide(tid,aid,91001,'approve',snapshot());await store.recover(startup=True)
    r=await store.report(tid,91001)
    assert r['error']['code']=='auth_context_lost' and not r['execution_attempts']


@pytest.mark.asyncio
async def test_c27_queue_full_keeps_pending(database):
    cfg,store=database;tid,aid=await pending(database);cfg.queue_capacity=0
    with pytest.raises(HTTPException) as e:await store.decide(tid,aid,91001,'approve',snapshot())
    assert e.value.status_code==429
    assert (await store.report(tid,91001))['approvals'][0]['status']=='pending'


def broker_fixture(tmp_path):
    from drill.broker import Broker
    assets=registry();policy={'project':'fixture-ma-m3','network':'fixture-internal'}
    key=tmp_path/'key';key.write_text('k'*32)
    info={'Id':'b'*64,'Image':'sha256:'+'c'*64,'Mounts':[],
          'Config':{'Labels':{'com.docker.compose.project':'fixture-ma-m3','com.docker.compose.service':'drill','powersystem.milestone':'MA-M3'},
                    'User':'65532:65532','Env':['PATH=/usr/bin'],'Healthcheck':{'Test':['CMD','health']},'Cmd':['health'],'Entrypoint':None},
          'HostConfig':{'ReadonlyRootfs':True,'Privileged':False,'RestartPolicy':{'Name':'no'},'CapDrop':['ALL'],
                        'SecurityOpt':['no-new-privileges:true'],'NetworkMode':'fixture-internal'},
          'State':{'Status':'running','Running':True,'Health':{'Status':'healthy'},'StartedAt':'2026-10-12T00:00:00Z'}}
    class FixedDocker:
        version='/v1.51'
        def get(self,path):
            assert path=='/v1.51/containers/'+'b'*64+'/json'
            return info
    cfg={'registry':{'assets':assets},'write_key_file':str(key),'read_key_file':str(key),
         'state_file':str(tmp_path/'attempts.sqlite'),'asset_id':'fixture-drill','container_id':'b'*64,
         'image_digest':'sha256:'+'c'*64,'policy':policy,'write_enabled':True,'window_until':time.time()+60}
    return Broker(cfg,FixedDocker()),info,cfg


@pytest.mark.parametrize('mutation',['volume','writable','privileged','auto_restart','business_network','root','secret','wrong_project'])
def test_c31_broker_rejects_nonstateless_or_wrong_target(tmp_path,mutation):
    b,info,cfg=broker_fixture(tmp_path)
    if mutation=='volume':info['Mounts']=[{'Name':'business-volume'}]
    if mutation=='writable':info['HostConfig']['ReadonlyRootfs']=False
    if mutation=='privileged':info['HostConfig']['Privileged']=True
    if mutation=='auto_restart':info['HostConfig']['RestartPolicy']['Name']='always'
    if mutation=='business_network':info['HostConfig']['NetworkMode']='business'
    if mutation=='root':info['Config']['User']='0'
    if mutation=='secret':info['Config']['Env'].append('API_KEY=never-external')
    if mutation=='wrong_project':info['Config']['Labels']['com.docker.compose.project']='other-project'
    with pytest.raises(ValueError):b.snapshot('fixture-drill')


def test_c29_c31_broker_one_post_and_durable_replay_guard(tmp_path,monkeypatch):
    from drill import broker as module
    b,info,cfg=broker_fixture(tmp_path);before=b.snapshot('fixture-drill');requests=[]
    class Restart:
        def __init__(self,*a,**k):pass
        def request(self,method,path,body):
            requests.append((method,path));info['State']['StartedAt']='2026-10-12T00:00:01Z'
        def getresponse(self):
            return type('Reply',(),{'status':204,'read':lambda *_:b''})()
        def close(self):pass
    monkeypatch.setattr(module,'UnixHTTP',Restart)
    g=grant();g.update({k:before[k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version')})
    token=sign(g,'k'*32);reply=b.restart(token)
    assert reply['after']['started_at']!=reply['before']['started_at']
    with pytest.raises(ValueError,match='dispatch_replayed'):b.restart(token)
    # A new process using the same durable state also cannot dispatch again.
    b2=module.Broker(cfg,b.docker)
    with pytest.raises(ValueError,match='dispatch_replayed'):b2.restart(token)
    assert requests==[('POST','/v1.51/containers/'+'b'*64+'/restart?t=10')]


def test_c30_broker_lost_response_never_posts_again(tmp_path,monkeypatch):
    from drill import broker as module
    b,info,cfg=broker_fixture(tmp_path);s=b.snapshot('fixture-drill');requests=[]
    class Lost:
        def __init__(self,*a,**k):pass
        def request(self,*a,**k):requests.append(a)
        def getresponse(self):raise TimeoutError()
        def close(self):pass
    monkeypatch.setattr(module,'UnixHTTP',Lost)
    g=grant();g.update({k:s[k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version')})
    token=sign(g,'k'*32)
    with pytest.raises(ValueError,match='execution_uncertain'):b.restart(token)
    with pytest.raises(ValueError,match='dispatch_replayed'):b.restart(token)
    assert len(requests)==1


def test_c26_broker_window_or_switch_closed_zero_post(tmp_path):
    b,info,cfg=broker_fixture(tmp_path);s=b.snapshot('fixture-drill');g=grant()
    g.update({k:s[k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version')})
    cfg['write_enabled']=False
    with pytest.raises(ValueError,match='write_disabled'):b.restart(sign(g,'k'*32))
    cfg['write_enabled']=True;cfg['window_until']=0
    with pytest.raises(ValueError,match='write_disabled'):b.restart(sign(g,'k'*32))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['disconnect','unsafe_output'])
async def test_c30_dispatch_response_loss_saves_uncertain_without_second_request(database,failure):
    import httpx
    from types import SimpleNamespace
    from ops_agent.write import execute
    cfg,store=database;tid,aid,row,a,cid=await approved(database)
    # begin_write is normally called by execute; remove only this fixture's unconsumed call.
    await store.pool.execute('DELETE FROM ma_tool_calls WHERE call_id=$1',cid)
    requests=[]
    async def handler(req):
        if req.url.path.endswith('/snapshot'):return httpx.Response(200,json={'snapshot':snapshot(),'policy':{'version':'fixture-m3-v1'}})
        assert req.url.path.endswith('/writes');requests.append(req)
        g=verify(json.loads(req.content)['grant'],cfg.executor_write_key)
        await store.consume_write(g)
        if failure=='unsafe_output':return httpx.Response(200,json={'status':'error','data':{'raw_secret':'do-not-persist'},'error':{'code':'bad','message':'do-not-persist','retryable':False}})
        raise httpx.ReadTimeout('fixture response lost')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async def identity(jwt):return {'user_id':91001,'role':'admin'}
        executor=SimpleNamespace(cfg=cfg,store=store,client=client,identity=identity)
        await execute(executor,row,row['execution_epoch'],a,'fixture-jwt',{'tool_calls':[],'sources':[],'warnings':[]})
    report=await store.report(tid,91001)
    assert report['status']=='failed' and report['error']['code']=='execution_uncertain'
    assert len(requests)==1 and report['execution_attempts'][0]['status']=='execution_uncertain'
    assert 'do-not-persist' not in json.dumps(report)


@pytest.mark.asyncio
async def test_c26_current_role_changed_before_write_zero_request(database):
    import httpx
    from types import SimpleNamespace
    from ops_agent.write import execute
    cfg,store=database;tid,aid,row,a,cid=await approved(database);requests=[]
    async def identity(jwt):return {'user_id':91001,'role':'operator'}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:requests.append(r))) as client:
        executor=SimpleNamespace(cfg=cfg,store=store,client=client,identity=identity)
        with pytest.raises(PermissionError):await execute(executor,row,row['execution_epoch'],a,'fixture-jwt',{'tool_calls':[]})
    assert not requests and not (await store.report(tid,91001))['execution_attempts']


@pytest.mark.asyncio
async def test_c26_audit_failure_blocks_approval_and_write(database,monkeypatch):
    import httpx
    from types import SimpleNamespace
    from ops_agent import write
    cfg,store=database
    t=Task(protocol='ma/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),
        actor={'user_id':91001,'role':'admin'},intent='演练重启',user_request='请重启演练容器',operation='restart_drill_container',
        constraints={'max_risk_class':'R2'},created_at=datetime.now(timezone.utc))
    tid,_=await store.create(t);row=await store.claim(tid,'audit-fixture');await store.transition(tid,row['execution_epoch'],'executing')
    async def identity(jwt):return {'user_id':91001,'role':'admin'}
    async def read(*args):return {'health':'healthy'}
    monkeypatch.setattr(write,'audit',lambda *a:(_ for _ in ()).throw(OSError('fixture disk failure')))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'snapshot':snapshot(),'policy':{'version':'fixture-m3-v1'}}))) as client:
        executor=SimpleNamespace(cfg=cfg,store=store,client=client,identity=identity,call=read)
        with pytest.raises(OSError):await write.prepare(executor,row,row['execution_epoch'],1,{'asset_id':'fixture-drill'},'fixture-jwt',{'tool_calls':[]})
    assert not (await store.report(tid,91001))['approvals']
    assert await store.pool.fetchval('SELECT count(*) FROM ma_execution_attempts')==0


@pytest.mark.asyncio
async def test_c29_planner_cleanup_cannot_delete_fresh_approval_authentication(database):
    import httpx
    from ops_agent.worker import Worker
    from ops_agent.write import ApprovalPending
    cfg,store=database
    t=Task(protocol='ma/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),
        actor={'user_id':91001,'role':'admin'},intent='演练重启',user_request='请重启演练容器',operation='restart_drill_container',
        constraints={'max_risk_class':'R2'},created_at=datetime.now(timezone.utc))
    tid,_=await store.create(t);row=await store.claim(tid,'old-planner')
    old={'jwt':'old-jwt','verification':None};fresh={'jwt':'new-jwt','verification':None};contexts={tid:old}
    async def provider(*args):return {'role':'assistant','tool_calls':[{'id':'c','function':{'name':'docker__restart_drill_container','arguments':'{"asset_id":"fixture-drill"}'}}]}
    async with httpx.AsyncClient() as client:
        worker=Worker(cfg,store,None,client,contexts,provider)
        async def prepare(*args):
            await store.prepare_approval(tid,row['execution_epoch'],1,{'asset_id':'fixture-drill'},snapshot(),{'version':'fixture-m3-v1'},{'tool_calls':[]})
            contexts[tid]=fresh
            raise ApprovalPending()
        worker.executor.call=prepare
        await worker.run_task(row)
    assert contexts[tid] is fresh


@pytest.mark.asyncio
async def test_acceptance_scope_never_sends_business_or_knowledge_to_model(database):
    import httpx
    from ops_agent.executor import Executor
    from ops_agent.worker import Worker
    cfg,store=database;cfg.acceptance_drill_only=True;requests=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:requests.append(r))) as client:
        executor=Executor(cfg,store,client)
        with pytest.raises(ValueError,match='guard_blocked'):
            await executor.call({},1,1,'platform.get_device',{'device_id':1},'fixture-jwt',{})
        worker=Worker(cfg,store,None,client,{})
        assert {s['function']['name'] for s in worker.schemas}=={'assets__list','docker__inspect_container','docker__restart_drill_container'}
    assert not requests


@pytest.mark.asyncio
async def test_acceptance_supervisor_cannot_search_outside_authorized_gateway():
    from types import SimpleNamespace
    from rag_agent.ops import SupervisorTools
    called=[]
    class Original:
        async def execute(self,*a):called.append(a)
    tools=SupervisorTools(Original(),SimpleNamespace(cfg=SimpleNamespace(acceptance_drill_only=True)),0)
    assert {s['function']['name'] for s in tools.schemas}=={'dispatch_ops_task','get_ops_task'}
    assert 'error' in await tools.execute('search_knowledge','{}','fixture-jwt',{})
    assert 'error' in await tools.execute('web_search','{}','fixture-jwt',{})
    assert not called
