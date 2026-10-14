"""SEC-C20..24: M4 boundaries, signed service concurrency and isolated DB."""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from ops_agent.config import Config
from ops_agent.runtime_profile import context, check_context, environments
from ops_agent.runtime_reviews import task_policy, licensed_summary
from ops_agent.store import Store
from ops_agent.worker import Worker
from ops_agent.security import sign
from ops_agent.schemas import Task
from ops_agent.inspection import select_assets, make_plan
from security_agent.config import Config as SecConfig
from security_agent.main import create_app
from review_contract import ReviewRequest, digest, sign_message, M4_POLICY, M4_LOCAL_SCOPE, M4_SERVER_SCOPE, M4_SERVER_POLICY
from test_runtime_gates import assets, Provider

def cfg_local():
    return Config('postgresql://u:p@127.0.0.1:15435/sec_m4_regression_test','redis://127.0.0.1:16380/6',
        'T'*40,'A'*40,'E'*40,backend_url='http://127.0.0.1:18106',supervisor_url='http://127.0.0.1:18098',
        runtime_profile='m4-local',enabled=True,infra_enabled=True,security_enabled=True,inspection_enabled=True,
        executor_url='http://127.0.0.1:8116',executor_token='X'*40,executor_grant_key='G'*40,
        security_url='http://127.0.0.1:8114',security_service_token='S'*40,security_request_key='Q'*40,security_result_key='R'*40)

@pytest.mark.parametrize('field,value', [('runtime_profile','unknown'),('database_url','postgresql://u:p@127.0.0.1:15435/powersystem'),('security_url','http://evil:8100'),('infra_enabled',False),('security_enabled',False)])
def test_C20_explicit_local_profile(field,value):
    cfg=cfg_local();setattr(cfg,field,value)
    with pytest.raises(ValueError):cfg.validate()

def test_C20_local_and_server_credential_boundaries():
    cfg=cfg_local();cfg.validate();cfg.write_enabled=True;cfg.executor_write_key='W'*40;cfg.validate()
    cfg.runtime_profile='m4-server-readonly';cfg.database_url='postgresql://u:p@postgres:5432/ma_ops'
    cfg.backend_url='http://api:8080';cfg.supervisor_url='http://agent-rag:8093';cfg.executor_url='http://ops-executor:8095';cfg.security_url='http://security-agent:8100'
    with pytest.raises(ValueError,match='server_read_only'):cfg.validate()
    cfg.write_enabled=False;cfg.executor_write_key='';cfg.validate()
    assert environments(cfg)==('production',)
    assert context(cfg)['scope_version']==M4_SERVER_SCOPE

def test_C22_legacy_context_cannot_be_upgraded():
    cfg=cfg_local()
    with pytest.raises(ValueError,match='security_review_stale'):check_context({'runtime_context':None},cfg)
    with pytest.raises(ValueError):check_context({'runtime_context':{**context(cfg),'scope_version':M4_SERVER_SCOPE}},cfg)

def test_C22_restore_requires_no_execution_or_model_credentials():
    cfg=cfg_local();cfg.runtime_profile='m4-restore-readonly';cfg.database_url='postgresql://r:p@127.0.0.1:15435/ma_m4_restore_test';cfg.redis_prefix='ma:restore:test:'
    cfg.enabled=cfg.infra_enabled=cfg.security_enabled=cfg.inspection_enabled=False
    cfg.executor_token=cfg.executor_grant_key='';cfg.validate()
    cfg.llm_key='K'*40
    with pytest.raises(ValueError,match='credential_free_restore'):cfg.validate()

def request(scope=M4_LOCAL_SCOPE):
    return dict(protocol='ma-review/v1',review_id=str(uuid4()),task_id=str(uuid4()),trace_id=str(uuid4()),step_no=1,execution_epoch=1,
        kind='sensitive_read',actor={'user_id':91001,'role':'admin'},tool={'name':'docker.inspect_container','version':'1.0','risk_class':'R0','args_hash':'a'*64,'plan_hash':'b'*64},
        asset={'asset_id':'fixture-drill','version':1,'identity_hash':'c'*64,'project_scope':'powersystem','criticality':'drill','verified':True},
        policy_version=M4_SERVER_POLICY if scope==M4_SERVER_SCOPE else M4_POLICY,scope_version=scope,normalized_intent='审查登记项目安全字段读取',evidence=[{'ref':'P1','source':'program','collected_at':datetime.now(timezone.utc).isoformat(),'complete':True,'filtered':True,'truncated':False,'facts':[{'key':'identity_verified','value':True}]}],deadline=(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat())

def headers(raw):
    req=ReviewRequest.model_validate(raw)
    return {'X-SEC-Service-Token':'S'*40,'X-SEC-Request':sign_message('Q'*40,'ma-review-request/v1',req.review_id,digest(req.model_dump(mode='json')),int(req.deadline.timestamp()))}

def test_C20_server_scope_never_accepts_r2():
    raw=request(M4_SERVER_SCOPE);raw['kind']='write_plan';raw['tool']['risk_class']='R2'
    with pytest.raises(ValidationError,match='server_read_only'):ReviewRequest.model_validate(raw)

@pytest.mark.asyncio
async def test_C20_service_rejects_wrong_deployment_scope_before_model():
    provider=Provider();app=create_app(SecConfig('S'*40,'Q'*40,'R'*40,'K'*40,enabled=True,model='deepseek-v4-flash-0731',runtime_profile='m4-local'),provider)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://security') as client:
        raw=request(M4_SERVER_SCOPE);r=await client.post('/internal/ma-review/v1/reviews',json=raw,headers=headers(raw))
        assert r.status_code==403 and not provider.calls

@pytest.mark.asyncio
async def test_C22_user_and_global_capacity_release_after_cancellation():
    entered=asyncio.Event();release=asyncio.Event()
    class Slow(Provider):
        async def review(self,value,timeout):
            entered.set();await release.wait();return await super().review(value,timeout)
    provider=Slow();app=create_app(SecConfig('S'*40,'Q'*40,'R'*40,'K'*40,enabled=True,model='deepseek-v4-flash-0731',runtime_profile='m4-local'),provider)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://security') as client:
        a=request();first=asyncio.create_task(client.post('/internal/ma-review/v1/reviews',json=a,headers=headers(a)));await entered.wait()
        b=request();r=await client.post('/internal/ma-review/v1/reviews',json=b,headers=headers(b));assert r.status_code==429
        b['actor']['user_id']=91002;second=asyncio.create_task(client.post('/internal/ma-review/v1/reviews',json=b,headers=headers(b)))
        for _ in range(100):
            if len(app.state.active)==2:break
            await asyncio.sleep(.001)
        c=request();c['actor']['user_id']=91003;r=await client.post('/internal/ma-review/v1/reviews',json=c,headers=headers(c));assert r.status_code==429
        first.cancel();await asyncio.gather(first,return_exceptions=True)
        assert len(app.state.active)==1 and 91001 not in app.state.users
        release.set();assert (await second).status_code==200
        assert (await client.post('/internal/ma-review/v1/reviews',json=c,headers=headers(c))).status_code==200
        assert not app.state.active and not app.state.users

@pytest_asyncio.fixture
async def environment(tmp_path,monkeypatch):
    path=os.getenv('SEC_M4_TEST_ENV_FILE')
    if not path:pytest.skip('M4 dedicated regression configuration required')
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        key,value=line.split('=',1);monkeypatch.setenv(key,value)
    cfg=Config.load();assert cfg.database_url.endswith('/sec_m4_regression_test');cfg.audit_dir=tmp_path
    store=await Store.open(cfg);await store.pool.execute('TRUNCATE ma_tasks,ma_assets CASCADE')
    registry=assets();await store.import_assets(registry,digest(registry),91001)
    provider=Provider();counts={'reads':0}
    sec=create_app(SecConfig(cfg.security_service_token,cfg.security_request_key,cfg.security_result_key,'K'*40,enabled=True,model='deepseek-v4-flash-0731',runtime_profile='m4-local'),provider)
    async with sec.router.lifespan_context(sec),httpx.AsyncClient(transport=httpx.ASGITransport(app=sec),base_url='http://security') as sec_client:
        async def transport(req):
            if req.url.path=='/internal/ma/v1/actor':
                now=int(time.time());return httpx.Response(200,json={'data':{'context':sign({'aud':'ma-ops','kid':'ma-v1','iat':now,'exp':now+60,'user_id':91001,'role':'admin'},cfg.actor_key)}})
            if req.url.path=='/internal/ma-review/v1/reviews':return await sec_client.post(req.url.path,json=json.loads(req.content),headers=dict(req.headers))
            if req.url.path=='/internal/ma-exec/v1/calls':
                counts['reads']+=1;stamp=datetime.now(timezone.utc).isoformat()
                return httpx.Response(200,json={'status':'ok','data':{'vantage':'container','sampled_at':stamp,'collected_at':stamp,'truncated':False,'complete':True,'running':True,'state':'running','health':'healthy','restart_count':0,'image_digest':'sha256:'+'c'*64,'started_at':stamp,'finished_at':stamp,'exit_code':0},'error':None})
            raise AssertionError(str(req.url))
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            worker=Worker(cfg,store,None,client,{})
            yield cfg,store,worker,provider,counts,registry
    await store.pool.close()

async def task(store):
    t=Task(protocol='ma/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),actor={'user_id':91001,'role':'admin'},intent='查询演练容器',user_request='查询fixture-drill安全状态',created_at=datetime.now(timezone.utc))
    tid,_=await store.create(t);row=await store.claim(tid,'m4-fixture');await store.transition(tid,row['execution_epoch'],'executing');return row

@pytest.mark.asyncio
async def test_C20_context_persists_and_signed_gate_binds(environment):
    cfg,store,worker,provider,counts,registry=environment;row=await task(store)
    assert check_context(row,cfg)==context(cfg)
    result={'tool_calls':[],'sources':[],'warnings':[],'diagnosis':{}};data=await worker.executor.call(row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result)
    assert 'error' not in data and counts['reads']==1
    report=await store.report(row['task_id'],91001)
    assert report['security_reviews'][0]['policy_version']==M4_POLICY
    assert provider.calls[0]['scope_version']==M4_LOCAL_SCOPE
    assert 'runtime_context' not in report

@pytest.mark.asyncio
async def test_C22_old_or_mutated_context_zero_collection(environment):
    cfg,store,worker,provider,counts,registry=environment;row=await task(store)
    await store.pool.execute("UPDATE ma_tasks SET runtime_context=NULL WHERE task_id=$1",row['task_id'])
    with pytest.raises(ValueError,match='security_review_stale'):
        await worker.executor.call(row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',{})
    assert not provider.calls and counts['reads']==0

@pytest.mark.asyncio
async def test_C20_security_deny_no_adapter_grant(environment):
    cfg,store,worker,provider,counts,registry=environment;provider.decisions['sensitive_read']='deny';row=await task(store)
    with pytest.raises(ValueError,match='security_denied'):
        await worker.executor.call(row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',{})
    assert counts['reads']==0

def test_C20_production_catalog_allowed_only_in_server_profile():
    cfg=cfg_local();registry=assets()[:2]
    for a in registry:a['environment']='production';a['criticality']='core';a['capabilities']=[c for c in a['capabilities'] if c!='docker.restart_drill_container']
    envelope={'targets':[],'user_request':'巡检本项目'}
    with pytest.raises(ValueError,match='scope_blocked'):select_assets(registry,envelope,cfg)
    cfg.runtime_profile='m4-server-readonly';assert len(select_assets(registry,envelope,cfg))==2
    with pytest.raises(ValueError,match='scope_too_large'):make_plan(registry,1)

@pytest.mark.parametrize('metric',[float('inf'),-1,'private material'])
def test_C23_nonfinite_negative_or_text_metrics_blocked(metric):
    from ops_agent.runtime_reviews import validate_payload
    payload={'task_status':'executing','evidence':[{'tool':'linux.get_cpu_usage','metrics':{'cpu_percent':metric}}],'actions':[]}
    with pytest.raises(ValueError):validate_payload(payload,cfg_local())

def test_C23_metrics_are_m4_only_and_unknown_fields_never_leave():
    cfg=cfg_local();report={'status':'executing','infrastructure_evidence':[{'evidence_ref':'call:x','call_id':'x','tool':'linux.get_cpu_usage','status':'ok'}],'tool_calls':[{'call_id':'x','result':{'cpu_percent':12.5,'load_1m':1.2,'logical_cpus':4,'sample_seconds':1,'environment':'unknown-raw-content'}}]}
    safe=licensed_summary(report,cfg)
    assert safe['evidence'][0]['metrics']['cpu_percent']==12.5
    assert 'unknown-raw-content' not in json.dumps(safe)
    assert 'metrics' not in licensed_summary(report)['evidence'][0]
    from ops_agent.runtime_reviews import validate_payload
    with pytest.raises(ValueError):validate_payload(safe)

@pytest.mark.asyncio
async def test_C20_model_catalog_excludes_unregistered_and_other_environment_capabilities(environment):
    cfg,store,worker,provider,counts,registry=environment
    await store.pool.execute("UPDATE ma_assets SET capabilities=CASE WHEN asset_id='fixture-drill' THEN '[\"docker.inspect_container\"]'::jsonb ELSE '[]'::jsonb END")
    schemas=await worker.available_schemas();names={s['function']['name'].replace('__','.') for s in schemas}
    assert 'assets.list' in names and 'docker.inspect_container' in names
    assert 'docker.get_container_logs' not in names and 'linux.get_cpu_usage' not in names
    await store.pool.execute("UPDATE ma_assets SET environment='production'")
    names={s['function']['name'].replace('__','.') for s in await worker.available_schemas()}
    assert 'docker.inspect_container' not in names and counts['reads']==0

@pytest.mark.parametrize('field,value',[('max_calls',8),('model','changed-model'),('write_enabled',True),('security_result_key','changed-result-key'),('actor_key','changed-actor-key'),('audit_dir',Path('changed-audit'))])
def test_C22_configuration_change_invalidates_saved_private_context(field,value):
    cfg=cfg_local();saved=context(cfg)
    assert len(saved['configuration_hash'])==64 and 'A'*40 not in json.dumps(saved)
    setattr(cfg,field,value)
    with pytest.raises(ValueError,match='security_review_stale'):check_context({'runtime_context':saved},cfg)

def test_C22_pre_digest_m4_history_not_promoted():
    cfg=cfg_local();saved=context(cfg);saved.pop('configuration_hash')
    with pytest.raises(ValueError,match='security_review_stale'):check_context({'runtime_context':saved},cfg)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_policy,old_scope",[("sec-m4-policy/v2","sec-m4-project-readonly/v2"),("sec-m4-policy/v3","sec-m4-project-readonly/v3"),("sec-m4-policy/v4","sec-m4-project-readonly/v4")])
async def test_C23_previous_server_policy_is_rejected_without_model_or_permission_promotion(old_policy,old_scope):
    provider=Provider();app=create_app(SecConfig("S"*40,"Q"*40,"R"*40,"K"*40,enabled=True,model="deepseek-v4-flash-0731",runtime_profile="m4-server-readonly"),provider)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://security") as client:
        raw=request(M4_SERVER_SCOPE);raw["policy_version"]=old_policy;raw["scope_version"]=old_scope
        r=await client.post("/internal/ma-review/v1/reviews",json=raw,headers=headers(raw))
        assert r.status_code==403 and not provider.calls
