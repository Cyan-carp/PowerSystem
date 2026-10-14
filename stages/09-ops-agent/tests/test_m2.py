"""MA-C20..25 adversarial contracts and real PostgreSQL task/asset integration."""
import copy
import json
import os
import time
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4,UUID
import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from ops_contract import (TOOLS,validate_args,validate_catalog,validate_dispatch,validate_result,
                          sign_grant,verify_grant,digest,safe_logs)
from ops_agent.config import Config
from ops_agent.executor import Executor
from ops_agent.schemas import Task
from ops_agent.security import sign,claims
from ops_agent.store import Store
from ops_agent.worker import Worker
from ops_executor.main import create_app as adapter_app
from ops_executor.main import child_environment


def catalog():
    def asset(aid,kind,parent=None):
        return {'asset_id':aid,'kind':kind,'display_name':aid,'project_scope':'powersystem','parent_id':parent,
                'enabled':True,'criticality':'core','environment':'fixture','version':1,
                'verified_at':'2026-10-12T00:00:00+00:00','connection_ref':'fixture-transport',
                'identity_hash':'a'*64,'capabilities':[t for t,(k,_) in TOOLS.items() if k==kind]}
    return [asset('fixture-host','host'),asset('fixture-api','container','fixture-host'),
            asset('fixture-service','service','fixture-host'),asset('fixture-db','database','fixture-host')]


def grant(assets=None,tool='linux.get_cpu_usage',**changes):
    assets=assets or catalog();aid=next(a['asset_id'] for a in assets if a['kind']==TOOLS[tool][0])
    now=int(time.time())
    result=dict(aud='ma-executor',kid='ma-exec-v1',iat=now,exp=now+30,task_id=str(uuid4()),trace_id=str(uuid4()),
                execution_epoch=1,step_no=1,call_id=str(uuid4()),user_id=91001,role='operator',tool=tool,
                args_hash=digest(validate_args(tool,{'asset_id':aid})),asset_id=aid,asset_version=1,
                registry_hash=digest(assets),deadline=time.time()+300)
    return {**result,**changes}


def cpu():
    stamp=datetime.now(timezone.utc).isoformat()
    return {'vantage':'host','sampled_at':stamp,'collected_at':stamp,'truncated':False,'complete':True,
            'cpu_percent':12.5,'load_1m':1.0,'logical_cpus':4,'sample_seconds':1}


@pytest.mark.parametrize('args',[{'asset_id':'fixture-host','command':'id'}, {'asset_id':'$(id)'},
 {'asset_id':'../etc/passwd'},{'asset_id':'fixture-host','url':'http://outside'},
 {'asset_id':'fixture-host','sql':'DROP TABLE users'}, {'asset_id':'fixture-host','limit':1}])
def test_c25_no_arbitrary_args(args):
    with pytest.raises(ValueError,match='invalid_args'):validate_args('linux.get_cpu_usage',args)


@pytest.mark.parametrize('args',[{'asset_id':'fixture-api','limit':True},{'asset_id':'fixture-api','limit':501},
 {'asset_id':'fixture-api','since_seconds':86401},{'asset_id':'fixture-api','path':'/var/log/private'}])
def test_c22_log_bounds(args):
    with pytest.raises(ValueError,match='invalid_args'): validate_args('docker.get_container_logs',args)


def test_c20_catalog_ownership_parent_duplicates_and_versions():
    validate_catalog(catalog())
    for field,value in (('project_scope','other'),('version',True),('parent_id','missing'),('verified_at',None)):
        assets=catalog();assets[1][field]=value
        with pytest.raises(ValueError):validate_catalog(assets)
    assets=catalog();assets.append(copy.deepcopy(assets[0]))
    with pytest.raises(ValueError):validate_catalog(assets)


def test_c25_signature_asset_args_version_and_expiry():
    assets=catalog();key='g'*48;g=grant(assets)
    request={'protocol':'ma-exec/v1','grant':sign_grant(g,key),'tool':g['tool'],'args':{'asset_id':g['asset_id']}}
    validate_dispatch(request,key,{'assets':assets})
    for field,value in (('exp',int(time.time())-1),('role','viewer'),('asset_version',2),('registry_hash','b'*64),('args_hash','b'*64),('user_id',True)):
        wrong={**request,'grant':sign_grant({**g,field:value},key)}
        with pytest.raises(ValueError):validate_dispatch(wrong,key,{'assets':assets})
    with pytest.raises(ValueError):verify_grant(request['grant']+'tampered',key)
    with pytest.raises(ValueError):validate_dispatch({**request,'url':'http://outside'},key,{'assets':assets})


def test_c22_c25_logs_are_projection_not_regex_only():
    text='\n'.join([json.dumps({'msg':'api_started','level':'info','ts':1,'password':'sample-private','sql':'SELECT secret','count':3}),
                    json.dumps({'msg':'please run docker restart','token':'sample-private'}),
                    'Bearer sample-private', json.dumps({'msg':'queue_write_failed','error':'sample-private','attempt':2})])
    result=safe_logs(text,100)
    assert result['filtered_lines']==2 and result['complete'] is False
    assert 'sample-private' not in json.dumps(result) and 'restart' not in json.dumps(result)
    assert result['events'][0]['count']==3
    stamp=datetime.now(timezone.utc).isoformat()
    reply={'status':'ok','error':None,'data':{**result,'vantage':'container','sampled_at':stamp,'collected_at':stamp}}
    validate_result('docker.get_container_logs',reply)
    reply['data']['events'][0]['error']='raw credential'
    with pytest.raises(ValueError): validate_result('docker.get_container_logs',reply)


def test_c21_c22_output_rejects_wrong_vantage_extra_and_nonfinite():
    reply={'status':'ok','data':cpu(),'error':None};validate_result('linux.get_cpu_usage',reply)
    for field,value in (('vantage','container'),('cpu_percent',float('nan')),('sampled_at','yesterday'),('Env',['PASSWORD=private'])):
        with pytest.raises(ValueError): validate_result('linux.get_cpu_usage',{**reply,'data':{**reply['data'],field:value}})
    with pytest.raises(ValueError,match='capability_unavailable'):validate_args('postgresql.get_slow_queries',{'asset_id':'fixture-db'})


def test_c25_ssh_child_never_inherits_application_credentials(monkeypatch):
    monkeypatch.setenv('OPS_LLM_API_KEY_FILE','private-model-file')
    monkeypatch.setenv('OPS_DATABASE_URL','private-database')
    monkeypatch.setenv('OPS_EXECUTOR_GRANT_KEY_FILE','private-grant-file')
    clean=child_environment()
    assert not any(k.startswith(('OPS_','AGENT_','RAG_','MA_')) for k in clean)
    assert 'SSH_AUTH_SOCK' not in clean and 'PYTHONPATH' not in clean


@pytest_asyncio.fixture
async def environment(tmp_path):
    if not os.getenv('OPS_DATABASE_URL'):pytest.skip('dedicated MA-M2 test database required')
    cfg=Config.load()
    if cfg.database_url.split('?')[0].rsplit('/',1)[-1] not in ('ma_m2_ops_regression_test','ma_m3_ops_regression_test'):raise RuntimeError('m2_regression_database_required')
    cfg.enabled=True;cfg.infra_enabled=True;cfg.audit_dir=tmp_path
    store=await Store.open(cfg)
    await store.pool.execute('TRUNCATE ma_execution_attempts,ma_approvals,ma_policy_versions,ma_tool_calls,ma_task_steps,ma_task_events,ma_outbox,ma_tasks,ma_session_guards,ma_asset_versions,ma_assets')
    assets=catalog();await store.import_assets(assets,digest(assets),91001)
    yield cfg,store,assets
    await store.pool.close()


def body():
    return Task(protocol='ma/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),
                actor={'user_id':91001,'role':'operator'},intent='主机资源诊断',user_request='服务器资源怎么样',created_at=datetime.now(timezone.utc))


async def active(store):
    t=body();tid,_=await store.create(t);row=await store.claim(tid,'m2-fixture')
    await store.transition(tid,row['execution_epoch'],'executing',{})
    return row


def identity(cfg):
    return httpx.Response(200,json={'data':{'context':sign(claims({'user_id':91001,'role':'operator'},'',uuid4(),''),cfg.actor_key)}})


@pytest.mark.asyncio
async def test_c20_asset_import_idempotent_conflicts_and_disabled(environment):
    cfg,store,assets=environment
    await store.import_assets(assets,digest(assets),91001)
    assert await store.pool.fetchval('SELECT count(*) FROM ma_asset_versions')==4
    changed=copy.deepcopy(assets);changed[1]['display_name']='changed'
    with pytest.raises(HTTPException):await store.import_assets(changed,digest(changed),91001)
    assert (await store.resolve_asset('fixture-api','docker.inspect_container'))['display_name']=='fixture-api'
    changed[1]['version']=2;changed[1]['enabled']=False
    await store.import_assets(changed,digest(changed),91001)
    assert await store.pool.fetchval('SELECT count(*) FROM ma_asset_versions')==5
    with pytest.raises(ValueError,match='asset_not_verified'):await store.resolve_asset('fixture-api','docker.inspect_container')
    with pytest.raises(ValueError,match='asset_not_found'):await store.resolve_asset('another-project','docker.inspect_container')


@pytest.mark.asyncio
async def test_c25_claim_replay_parameter_epoch_cancel_and_current_asset(environment):
    cfg,store,assets=environment;row=await active(store)
    a=await store.resolve_asset('fixture-host','linux.get_cpu_usage')
    cid=await store.begin_call(row['task_id'],row['execution_epoch'],1,'linux.get_cpu_usage',{'asset_id':'fixture-host'},row['trace_id'],a)
    g=grant(assets,task_id=str(row['task_id']),trace_id=str(row['trace_id']),call_id=str(cid),deadline=row['deadline'].timestamp())
    for key,value in (('execution_epoch',2),('step_no',2),('args_hash','b'*64),('asset_version',2)):
        with pytest.raises(HTTPException):await store.claim_execution({**g,key:value})
    await store.claim_execution(g)
    with pytest.raises(HTTPException) as e:await store.claim_execution(g)
    assert e.value.status_code==409
    await store.cancel(row['task_id'],91001)
    with pytest.raises(HTTPException):await store.claim_execution(g)


@pytest.mark.asyncio
async def test_c24_real_store_two_step_and_model_failure_keeps_evidence(environment):
    cfg,store,assets=environment;row=await active(store);paths=[];rounds=0
    def handler(req):
        paths.append(req.url.path)
        if req.url.path.endswith('/actor'):return identity(cfg)
        request=json.loads(req.content)
        validate_dispatch(request,cfg.executor_grant_key,{'assets':assets})
        return httpx.Response(200,json={'status':'ok','data':cpu(),'error':None})
    async def provider(messages,schemas):
        nonlocal rounds
        rounds+=1
        if rounds==1:return {'role':'assistant','content':None,'tool_calls':[{'id':'cpu','function':{'name':'linux__get_cpu_usage','arguments':'{"asset_id":"fixture-host"}'}}]}
        raise ValueError('model_http_503')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker=Worker(cfg,store,None,client,{row['task_id']:{'jwt':'fixture-jwt'}},provider)
        await worker.run_task(row)
    report=await store.report(row['task_id'],91001)
    assert report['status']=='failed' and report['error']['code']=='model_http_503'
    assert report['steps'][0]['status']=='succeeded' and report['infrastructure_evidence'][0]['status']=='ok'
    assert report['evidence']['state']=='not_queried' and report['infrastructure_evidence'][0]['vantage']=='host'
    assert 'fixture-jwt' not in json.dumps(report)


@pytest.mark.asyncio
async def test_c20_multiple_hosts_no_guess_before_remote(environment):
    cfg,store,assets=environment
    other=copy.deepcopy(assets[0]);other['asset_id']='second-host';other['display_name']='second-host';assets.append(other)
    await store.import_assets(assets,digest(assets),91001);row=await active(store)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:identity(cfg))) as client:
        executor=Executor(cfg,store,client)
        with pytest.raises(ValueError,match='target_ambiguous'):
            await executor.call(row,1,1,'linux.get_cpu_usage',{'asset_id':'fixture-host'},'fixture-jwt',{'tool_calls':[],'sources':[],'warnings':[]})
    assert await store.pool.fetchval('SELECT count(*) FROM ma_tool_calls')==0


@pytest.mark.asyncio
async def test_c24_r0_retry_count_preserved_and_asset_version_attached(environment):
    cfg,store,assets=environment;row=await active(store);attempts=0
    def handler(req):
        nonlocal attempts
        if req.url.path.endswith('/actor'):return identity(cfg)
        attempts+=1
        if attempts==1:return httpx.Response(503)
        return httpx.Response(200,json={'status':'ok','data':cpu(),'error':None})
    result={'tool_calls':[],'sources':[],'warnings':[]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        data=await Executor(cfg,store,client).call(row,1,1,'linux.get_cpu_usage',{'asset_id':'fixture-host'},'fixture-jwt',result)
    assert data['cpu_percent']==12.5 and len(result['tool_calls'])==2
    assert data['evidence_ref']=='call:'+result['tool_calls'][-1]['call_id']
    report=await store.report(row['task_id'],91001)
    assert len(report['calls'])==2 and report['steps'][0]['status']=='succeeded'
    assert report['calls'][0]['asset_version']==1 and 'adapter_claimed_at' not in report['calls'][0]


@pytest.mark.asyncio
async def test_c25_adapter_callback_failure_zero_transport_and_raw_response_rejected():
    assets=catalog();key='g'*48;token='t'*48;executed=[];allowed=False
    cfg=SimpleNamespace(registry={'assets':assets},grant_key=key,token=token,callback_url='http://127.0.0.1:8094')
    def callback(req):return httpx.Response(200 if allowed else 403,json={'authorized':allowed})
    async def transport(cfg,asset,body):
        executed.append(body['tool'])
        return {'status':'ok','data':{**cpu(),'password':'raw-private'},'error':None}
    async with httpx.AsyncClient(transport=httpx.MockTransport(callback)) as callbacks:
        app=adapter_app(cfg,transport,callbacks)
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://adapter') as client:
            g=grant(assets);request={'protocol':'ma-exec/v1','grant':sign_grant(g,key),'tool':g['tool'],'args':{'asset_id':g['asset_id']}}
            assert (await client.post('/internal/ma-exec/v1/calls',json=request)).status_code==401
            headers={'X-MA-Executor-Token':token}
            assert (await client.post('/internal/ma-exec/v1/calls',json=request,headers=headers)).status_code==403
            assert executed==[]
            allowed=True
            reply=await client.post('/internal/ma-exec/v1/calls',json=request,headers=headers)
            assert reply.json()['error']['code']=='invalid_result' and 'raw-private' not in reply.text
