"""SEC-C07..14 real PostgreSQL gates; providers/collectors are explicitly fixtures."""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException

from ops_agent.config import Config
from ops_agent.store import Store
from ops_agent.schemas import Task
from ops_agent.worker import Worker
from ops_agent.security import sign, digest
from ops_agent.approvals import saved_plan
from ops_agent.write import prepare, ApprovalPending
from review_contract import RUNTIME_POLICY, RUNTIME_SCOPE, ReviewRequest
from security_agent.main import create_app
from security_agent.config import Config as ReviewConfig


def assets():
    def asset(aid,kind,parent,caps):
        return dict(asset_id=aid,kind=kind,display_name=aid,project_scope='powersystem',parent_id=parent,
            enabled=True,criticality='drill',environment='fixture',version=1,verified_at=datetime.now(timezone.utc).isoformat(),
            connection_ref='local-drill',identity_hash='a'*64,capabilities=caps)
    return [asset('fixture-host','host',None,[]),asset('fixture-drill','container','fixture-host',['docker.inspect_container','docker.get_container_logs','docker.restart_drill_container']),asset('fixture-service','service','fixture-host',['linux.get_system_logs']),asset('fixture-db','database','fixture-host',['postgresql.get_connection_stats'])]


def snapshot(registry):
    return dict(asset_id='fixture-drill',asset_version=1,registry_hash=digest(registry),container_id='b'*64,
        image_digest='sha256:'+'c'*64,config_hash='d'*64,policy_version=digest({'version':'fixture'}),
        state='running',running=True,health='healthy',started_at='2026-10-13T00:00:00+00:00',stateless_verified=True,
        collected_at=datetime.now(timezone.utc).isoformat())


class Provider:
    def __init__(self): self.calls=[];self.decisions={};self.error=False
    async def review(self,value,timeout):
        self.calls.append(value)
        if self.error: raise TimeoutError()
        return {'decision':self.decisions.get(value['kind'],'allow'),'reason':'限定程序证据与当前门禁一致。',
            'rule_hits':['SEC-R01'],'evidence_refs':[value['evidence'][0]['ref']]},{'total_tokens':10}


@pytest_asyncio.fixture
async def environment(tmp_path):
    if os.getenv('OPS_SECURITY_ENABLED')!='true': pytest.skip('dedicated SEC-M2 regression config required')
    cfg=Config.load()
    if not cfg.database_url.endswith('/sec_m2_regression_test'): raise RuntimeError('isolated_m2_regression_required')
    cfg.audit_dir=tmp_path;cfg.acceptance_drill_only=False
    store=await Store.open(cfg)
    await store.pool.execute('TRUNCATE ma_tasks,ma_assets CASCADE')
    registry=assets();await store.import_assets(registry,digest(registry),91001)
    provider=Provider();counts={'reads':0,'writes':0};role={'value':'admin'}
    sec=create_app(ReviewConfig(cfg.security_service_token,cfg.security_request_key,cfg.security_result_key,'K'*40,enabled=True,model='deepseek-v4-flash-0731'),provider)
    async with sec.router.lifespan_context(sec),httpx.AsyncClient(transport=httpx.ASGITransport(app=sec),base_url='http://127.0.0.1:8100') as sec_client:
        async def transport(req):
            path=req.url.path
            if path=='/internal/ma/v1/actor':
                now=int(time.time());return httpx.Response(200,json={'data':{'context':sign({'aud':'ma-ops','kid':'ma-v1','iat':now,'exp':now+60,'user_id':91001,'role':role['value']},cfg.actor_key)}})
            if path=='/internal/ma-review/v1/reviews':
                response=await sec_client.post(path,json=json.loads(req.content),headers=dict(req.headers))
                if getattr(provider,'bad_signature',False):response.headers['X-SEC-Result']='invalid'
                return response
            if path=='/internal/ma-exec/v1/calls':
                counts['reads']+=1;stamp=datetime.now(timezone.utc).isoformat()
                data=dict(vantage='container',sampled_at=stamp,collected_at=stamp,truncated=False,complete=True,state='running',running=True,restart_count=0,image_digest='sha256:'+'c'*64,health='healthy',started_at=stamp,finished_at=stamp,exit_code=0)
                return httpx.Response(200,json={'status':'ok','data':data,'error':None})
            if path=='/internal/ma-exec/v2/snapshot':
                assert 'gate' in json.loads(req.content)
                return httpx.Response(200,json={'snapshot':snapshot(registry),'policy':{'version':'fixture'}})
            if path=='/internal/ma-exec/v2/writes':
                from ops_write_contract import verify
                grant=verify(json.loads(req.content)['grant'],cfg.executor_write_key)
                await store.consume_write(grant);counts['writes']+=1
                before=snapshot(registry);after={**before,'started_at':'2026-10-13T01:00:00+00:00'}
                return httpx.Response(200,json={'status':'ok','data':{'status':'ok','before':before,'after':after,'action_confirmed':True},'error':None})
            if path=='/api/v1/devices/10': return httpx.Response(200,json={'code':0,'data':{'id':10,'device_code':'FIX10','name':'fixture'}})
            if path=='/api/v1/devices/10/telemetry/latest': return httpx.Response(200,json={'code':0,'data':{'device_id':10,'ts_ms':0,'power':1}})
            raise AssertionError('unexpected fixture request '+path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            worker=Worker(cfg,store,None,client,{})
            yield cfg,store,worker,provider,counts,role,registry
    await store.pool.execute('DROP TRIGGER IF EXISTS sec_m2_audit_failure ON ma_security_review_events')
    await store.pool.execute('DROP FUNCTION IF EXISTS sec_m2_audit_failure()')
    await store.pool.close()


async def task(env,write=False):
    cfg,store,worker,*_=env
    t=Task(protocol='ma/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),
        actor={'user_id':91001,'role':'admin'},intent='本机隔离演练',user_request='请重启本机隔离演练容器' if write else '查询演练状态',
        operation='restart_drill_container' if write else 'read_only',constraints={'max_risk_class':'R2' if write else 'R0'},created_at=datetime.now(timezone.utc))
    tid,_=await store.create(t);row=await store.claim(tid,'sec-m2-fixture');await store.transition(tid,row['execution_epoch'],'executing')
    return row,{'tool_calls':[],'sources':[],'warnings':[],'diagnosis':{}}


@pytest.mark.parametrize('decision,code',[('deny','security_denied'),('needs_clarification','security_clarification_required')])
@pytest.mark.parametrize('tool,aid',[('docker.inspect_container','fixture-drill'),('docker.get_container_logs','fixture-drill'),('linux.get_system_logs','fixture-service'),('postgresql.get_connection_stats','fixture-db')])
async def test_C07_denied_read_zero_collector(environment,decision,code,tool,aid):
    _,store,w,p,counts,*_=environment;p.decisions['sensitive_read']=decision
    row,result=await task(environment)
    with pytest.raises(ValueError,match=code): await w.executor.call(row,row['execution_epoch'],1,tool,{'asset_id':aid},'fixture-jwt',result)
    assert counts['reads']==0 and await store.pool.fetchval('SELECT count(*) FROM ma_tool_calls')==0
    assert (await store.report(row['task_id'],91001))['security_reviews'][0]['decision']==decision


async def test_C07_allowed_read_has_signed_review_and_public_projection(environment):
    _,store,w,p,counts,*_=environment;row,result=await task(environment)
    await w.executor.call(row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result)
    assert counts['reads']==1
    public=await store.report(row['task_id'],91001)
    assert public['security_reviews'][0]['kind']=='sensitive_read' and len(public['security_events'])==2
    assert 'request' not in public['security_reviews'][0] and 'signature_validated' not in public['security_reviews'][0]
    with pytest.raises(HTTPException) as exc: await store.report(row['task_id'],999)
    assert exc.value.status_code==404


@pytest.mark.parametrize('kind',['write_plan','sensitive_read'])
async def test_C09_denied_before_approval_no_card_no_write(environment,kind):
    _,store,w,p,counts,*_=environment;p.decisions[kind]='deny';row,result=await task(environment,True)
    with pytest.raises(ValueError,match='security_denied'): await prepare(w.executor,row,row['execution_epoch'],1,{'asset_id':'fixture-drill'},'fixture-jwt',result)
    assert await store.pool.fetchval('SELECT count(*) FROM ma_approvals')==0
    assert await store.pool.fetchval('SELECT count(*) FROM ma_execution_attempts')==0


async def approved(environment):
    _,store,w,p,counts,role,registry=environment;row,result=await task(environment,True)
    with pytest.raises(ApprovalPending): await prepare(w.executor,row,row['execution_epoch'],1,{'asset_id':'fixture-drill'},'fixture-jwt',result)
    a=(await store.report(row['task_id'],91001))['approvals'][0]
    await store.decide(row['task_id'],__import__('uuid').UUID(a['approval_id']),91001,'approve',snapshot(registry))
    fresh=await store.claim(row['task_id'],'sec-m2-execution');await store.transition(row['task_id'],fresh['execution_epoch'],'executing')
    return fresh,await store.approved_plan(row['task_id']),result


@pytest.mark.parametrize('change',['expired','plan','epoch','role','policy','cancelled'])
async def test_C10_C11_final_gate_stale_zero_write(environment,change):
    _,store,w,p,counts,role,_=environment;row,a,result=await approved(environment)
    await store.security_gates.require(w.executor,row,row['execution_epoch'],a['step_no'],'docker.restart_drill_container',a['plan']['args'],'fixture-jwt',result,'write_preflight',plan=a['plan'])
    if change=='expired': await store.pool.execute("UPDATE ma_security_reviews SET expires_at=now()-interval '1 second'")
    if change=='plan': a['plan']['impact']='changed'
    if change=='epoch': row['execution_epoch']+=1
    if change=='role': role['value']='operator'
    if change=='policy': await store.pool.execute("UPDATE ma_security_reviews SET request=jsonb_set(request,'{policy_version}','\"wrong\"')")
    if change=='cancelled': await store.cancel(row['task_id'],91001)
    if change=='role':
        with pytest.raises(ValueError): await store.security_gates.require(w.executor,row,row['execution_epoch'],a['step_no'],'docker.restart_drill_container',a['plan']['args'],'fixture-jwt',result,'write_preflight',plan=a['plan'])
    else:
        with pytest.raises(Exception): await store.begin_write(row['task_id'],row['execution_epoch'],a)
    assert await store.pool.fetchval('SELECT count(*) FROM ma_execution_attempts')==0 and counts['writes']==0


async def test_C11_new_epoch_requires_new_review(environment):
    _,store,w,p,*_=environment;row,a,result=await approved(environment)
    with pytest.raises(ValueError,match='security_review_stale'): await store.begin_write(row['task_id'],row['execution_epoch'],a)
    assert len(p.calls)==2


async def test_C12_model_timeout_blocks_read(environment):
    _,store,w,p,counts,*_=environment;p.error=True;row,result=await task(environment)
    with pytest.raises(ValueError,match='security_unavailable'): await w.executor.call(row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result)
    assert counts['reads']==0 and (await store.report(row['task_id'],91001))['security_reviews'][0]['state']=='unavailable'


async def test_C12_invalid_result_signature_zero_read(environment):
    _,store,w,p,counts,*_=environment;p.bad_signature=True;row,result=await task(environment)
    with pytest.raises(ValueError,match='security_invalid'):await w.executor.call(row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result)
    assert counts['reads']==0 and (await store.report(row['task_id'],91001))['security_reviews'][0]['state']=='invalid'


async def test_C12_cancel_during_review_zero_read(environment):
    _,store,w,p,counts,*_=environment;row,result=await task(environment);review=p.review
    async def cancelled(value,timeout):
        await store.cancel(row['task_id'],91001)
        return await review(value,timeout)
    p.review=cancelled
    with pytest.raises(ValueError):await w.executor.call(row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result)
    assert counts['reads']==0 and await store.pool.fetchval('SELECT count(*) FROM ma_tool_calls')==0


async def test_C12_audit_transaction_failure_zero_model(environment):
    _,store,w,p,counts,*_=environment;row,result=await task(environment)
    await store.pool.execute("CREATE FUNCTION sec_m2_audit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture audit unavailable'; END $$")
    await store.pool.execute('CREATE TRIGGER sec_m2_audit_failure BEFORE INSERT ON ma_security_review_events FOR EACH ROW EXECUTE FUNCTION sec_m2_audit_failure()')
    with pytest.raises(Exception): await w.executor.call(row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result)
    assert counts['reads']==0 and len(p.calls)==0 and await store.pool.fetchval('SELECT count(*) FROM ma_security_reviews')==0
    assert await store.pool.fetchval('SELECT count(*) FROM ma_security_gate_contexts')==0


@pytest.mark.parametrize('mutation',['payload','receiver','expiry','asset','epoch'])
async def test_C08_final_send_rechecks_binding(environment,mutation):
    _,store,w,p,*_=environment;row,result=await task(environment)
    payload={'task_status':'executing','evidence':[],'actions':[]}
    rid=await store.security_gates.require(w.executor,row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result,'data_egress',payload=payload)
    hash=digest(payload);receiver='operation'
    if mutation=='payload':hash=digest({'changed':True})
    if mutation=='receiver':receiver='supervisor'
    if mutation=='expiry':await store.pool.execute("UPDATE ma_security_reviews SET expires_at=now()-interval '1 second'")
    if mutation=='asset':await store.pool.execute("UPDATE ma_assets SET version=2 WHERE asset_id='fixture-drill'")
    if mutation=='epoch':await store.pool.execute('UPDATE ma_tasks SET execution_epoch=execution_epoch+1')
    with pytest.raises(ValueError):await store.security_gates.check_egress(row['task_id'],rid,row['user_id'],hash,receiver)


async def test_C10_security_policy_change_invalidates_approval(environment):
    _,store,w,p,*_=environment;row,a,result=await approved(environment)
    from ops_agent.runtime_reviews import contract
    original=contract.RUNTIME_POLICY
    try:
        contract.RUNTIME_POLICY='sec-m2-policy/future'
        from ops_agent.write import execute
        with pytest.raises(ValueError,match='approval_invalidated'):await execute(w.executor,row,row['execution_epoch'],a,'fixture-jwt',result)
        assert await store.pool.fetchval('SELECT count(*) FROM ma_execution_attempts')==0
    finally:contract.RUNTIME_POLICY=original


@pytest.mark.parametrize('post_result',['deny','timeout'])
async def test_C12_post_failure_keeps_executed_fact(environment,post_result):
    _,store,w,p,counts,*_=environment;row,a,result=await approved(environment)
    review=p.review
    async def fixture(value,timeout):
        if value['kind']=='post_execution':
            if post_result=='timeout':raise TimeoutError()
            p.decisions['post_execution']='deny'
        return await review(value,timeout)
    p.review=fixture
    from ops_agent.write import execute
    await execute(w.executor,row,row['execution_epoch'],a,'fixture-jwt',result)
    report=await store.report(row['task_id'],91001)
    assert report['status']=='succeeded' and counts['writes']==1
    assert report['execution_attempts'][0]['status']=='succeeded'
    assert any('后审' in x for x in report['warnings'])
    assert report['security_reviews'][-1]['decision']==('deny' if post_result=='deny' else None)


@pytest.mark.parametrize('secret',['Bearer private-token','password=hidden','postgresql://user:pass@host/db'])
async def test_C08_unlicensed_material_zero_external(environment,secret):
    _,store,w,p,*_=environment;row,result=await task(environment)
    with pytest.raises(ValueError): await store.security_gates.require(w.executor,row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result,'data_egress',payload={'secret':secret})
    assert len(p.calls)==0


async def test_C08_egress_binds_payload_and_receiver(environment):
    _,store,w,p,*_=environment;row,result=await task(environment)
    payload={'task_status':'executing','evidence':[],'actions':[]}
    await store.security_gates.require(w.executor,row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result,'data_egress',payload=payload,receiver='supervisor')
    assert p.calls[0]['egress']['payload_hash']==digest(payload)
    assert p.calls[0]['egress']['receiver']=='supervisor'
    assert p.calls[0]['scope_version']==RUNTIME_SCOPE and p.calls[0]['policy_version']==RUNTIME_POLICY


async def test_C12_pending_recovery_does_not_call_model(environment):
    _,store,w,p,*_=environment;row,result=await task(environment)
    rid=await store.security_gates.require(w.executor,row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result,'sensitive_read')
    await store.pool.execute("UPDATE ma_security_reviews SET state='pending' WHERE review_id=$1",rid)
    count=len(p.calls);assert await store.security_gates.controller.recover()==1
    assert len(p.calls)==count and (await store.security_gates.controller.get(rid,91001))['state']=='unavailable'


async def test_C11_final_consumption_race_only_one_attempt(environment):
    _,store,w,p,counts,role,_=environment;row,a,result=await approved(environment)
    await store.security_gates.require(w.executor,row,row['execution_epoch'],a['step_no'],'docker.restart_drill_container',a['plan']['args'],'fixture-jwt',result,'write_preflight',plan=a['plan'],facts={'approval_status':'approved','approval_valid':True,'approval_current_epoch':row['execution_epoch'],'plan_unchanged':True,'policy_current':True})
    cid=await store.begin_write(row['task_id'],row['execution_epoch'],a)
    current=await store.get(row['task_id']);now=int(time.time())
    g={'aud':'ma-drill-executor','kid':'ma-write-v1','action':'restart','iat':now,'exp':now+30,
       'task_id':str(row['task_id']),'trace_id':str(row['trace_id']),'execution_epoch':row['execution_epoch'],
       'step_no':a['step_no'],'call_id':str(cid),'user_id':91001,'role':'admin','args_hash':a['args_hash'],
       'deadline':current['deadline'].timestamp(),'approval_id':str(a['approval_id']),
       **{k:a['plan']['snapshot'][k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version')}}
    replies=await asyncio.gather(store.consume_write(g),store.consume_write(g),return_exceptions=True)
    assert sum(isinstance(r,dict) and r.get('authorized') for r in replies)==1
    assert await store.pool.fetchval('SELECT count(*) FROM ma_execution_attempts')==1


async def test_C13_fixed_verification_zero_all_models(environment):
    _,store,w,p,*_=environment;row,result=await task(environment)
    from ops_agent.schemas import Verification
    w.contexts[row['task_id']]={'jwt':'fixture-jwt','verification':Verification(session_id=row['session_id'],parent_turn_id=1,device_id=10,checks=['latest_telemetry'],verification_id=uuid4())}
    await w.run_task(row)
    report=await store.report(row['task_id'],91001)
    assert report['status']=='succeeded' and report['model_calls']==0 and not report['security_reviews'] and len(p.calls)==0


async def test_C12_disabling_service_is_not_a_gate_bypass(environment):
    _,store,w,p,counts,*_=environment;row,result=await task(environment)
    store.security_gates=None
    with pytest.raises(ValueError,match='security_unavailable'): await w.executor.call(row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result)
    assert counts['reads']==0 and len(p.calls)==0


@pytest.mark.parametrize('payload',[{'task_status':'executing','evidence':[],'actions':[],'extra':'unknown'},
    {'task_status':'executing','evidence':[{'state':{'command':'inspect'}}],'actions':[]},
    {'task_status':'executing','evidence':[],'actions':[{'status':'restart'}]}])
async def test_C08_unknown_projection_zero_external(environment,payload):
    _,store,w,p,*_=environment;row,result=await task(environment)
    with pytest.raises(ValueError,match='security_projection_blocked'):
        await store.security_gates.require(w.executor,row,row['execution_epoch'],1,'docker.inspect_container',{'asset_id':'fixture-drill'},'fixture-jwt',result,'data_egress',payload=payload)
    assert len(p.calls)==0
