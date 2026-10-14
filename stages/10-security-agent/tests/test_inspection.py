"""SEC-C07..14 real PostgreSQL gates; providers/collectors are explicitly fixtures."""
import asyncio
import json
import os
import time
from pathlib import Path
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
        if self.error or getattr(self,'fail_kind',None)==value['kind']: raise TimeoutError()
        return {'decision':self.decisions.get(value['kind'],'allow'),'reason':'限定程序证据与当前门禁一致。',
            'rule_hits':['SEC-R01'],'evidence_refs':[value['evidence'][0]['ref']]},{'total_tokens':10}


@pytest_asyncio.fixture
async def environment(tmp_path,monkeypatch):
    if not os.getenv('SEC_M3_TEST_ENV_FILE'): pytest.skip('dedicated SEC-M2 regression config required')
    for line in Path(os.environ['SEC_M3_TEST_ENV_FILE']).read_text(encoding='utf-8').splitlines():
        key,value=line.split('=',1);monkeypatch.setenv(key,value)
    cfg=Config.load()
    if not cfg.database_url.endswith('/sec_m3_regression_test'): raise RuntimeError('isolated_m2_regression_required')
    cfg.audit_dir=tmp_path;cfg.acceptance_drill_only=False
    store=await Store.open(cfg)
    await store.pool.execute('TRUNCATE ma_tasks,ma_assets CASCADE')
    registry=local_assets();registry[1]["capabilities"]=["docker.inspect_container"];await store.import_assets(registry,digest(registry),91001)
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
                counts['reads']+=1
                if counts.get('cancel_after_read'):
                    from ops_contract import verify_grant
                    grant=verify_grant(json.loads(req.content)['grant'],cfg.executor_grant_key)
                    await store.cancel(__import__('uuid').UUID(grant['task_id']),91001)
                stamp=datetime.now(timezone.utc).isoformat()
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


"""SEC-C15..19: program rules and real isolated PostgreSQL; models/collector fixtures."""
import copy
from datetime import timedelta
from pydantic import ValidationError
from ops_agent.inspection import select_assets, make_plan, evaluate, report_for
from ops_agent.schemas import Task
from ops_agent.runtime_reviews import validate_payload, licensed_summary
from review_contract import ReviewRequest, INSPECTION_POLICY, INSPECTION_SCOPE

def inspection_task(**kw):
    value=dict(protocol='ma/v1',task_id=uuid4(),trace_id=uuid4(),idempotency_key=uuid4(),session_id=uuid4(),actor={'user_id':91001,'role':'admin'},intent='项目只读巡检',user_request='巡检本项目',task_kind='security_inspection',created_at=datetime.now(timezone.utc))
    value.update(kw);return Task(**value)

def local_assets():
    return [a for a in assets() if a['kind'] in ('host','container')]

@pytest.mark.parametrize('question,target,count', [('巡检本项目',None,2),('巡检 fixture-drill',None,1),('巡检本项目','fixture-drill',1)])
def test_C15_scope(question,target,count):
    env={'user_request':question,'targets':[{'asset_id':None,'description':target}] if target else []}
    assert len(select_assets(local_assets(),env))==count

@pytest.mark.parametrize('mode,code',[('ambiguous','target_ambiguous'),('unknown','asset_not_found'),('disabled','asset_not_verified'),('unverified','asset_not_verified'),('production','inspection_scope_blocked'),('cross','inspection_scope_blocked')])
def test_C15_no_probe_scope(mode,code):
    registry=local_assets();env={'user_request':'巡检本项目','targets':[]}
    if mode=='ambiguous':env['user_request']='检查服务器'
    if mode=='unknown':env['targets']=[{'asset_id':'outside-project'}]
    if mode=='disabled':registry[1]['enabled']=False
    if mode=='unverified':registry[1]['verified_at']=None
    if mode=='production':registry[1]['environment']='production'
    if mode=='cross':registry[1]['project_scope']='outside'
    with pytest.raises(ValueError,match=code):select_assets(registry,env)

def test_C16_budget_and_missing_capabilities():
    registry=local_assets()
    with pytest.raises(ValueError,match='inspection_scope_too_large'):make_plan(registry,1)
    report=report_for(registry,datetime.now(timezone.utc).isoformat())
    assert {'permissions_exposure','credentials_egress','audit_recovery'}<={i['category'] for i in report['unchecked_items']}
    assert all(i['status']=='not_checked' for i in report['items'])

def evidence():
    now=datetime.now(timezone.utc)
    return now, {'evidence_ref':'call:fixture','asset_id':'fixture-drill','asset_version':1,'status':'ok','complete':True,'truncated':False,'sampled_at':now.isoformat(),'collected_at':now.isoformat(),'vantage':'container'}

@pytest.mark.parametrize('mode,expected',[('valid','passed'),('stopped','finding'),('unhealthy','finding'),('unconfigured','inconclusive'),('empty','inconclusive'),('truncated','inconclusive'),('incomplete','inconclusive'),('old','inconclusive'),('future','inconclusive'),('historical','inconclusive'),('version','inconclusive'),('no_time','inconclusive')])
def test_C17_evidence_rules(mode,expected):
    now,e=evidence();data={'complete':True,'running':True,'state':'running','health':'healthy'}
    if mode=='stopped':data['running']=False
    if mode=='unhealthy':data['health']='unhealthy'
    if mode=='unconfigured':data['health']='not_configured'
    if mode=='empty':data={}
    if mode=='truncated':e['truncated']=True
    if mode=='incomplete':e['complete']=False
    if mode=='old':e['sampled_at']=(now-timedelta(seconds=301)).isoformat()
    if mode=='future':e['sampled_at']=(now+timedelta(seconds=61)).isoformat()
    if mode=='historical':e['collected_at']=(now-timedelta(seconds=10)).isoformat()
    if mode=='version':e['asset_version']=2
    if mode=='no_time':e['sampled_at']=None
    assert evaluate('docker.inspect_container',data,e,(now-timedelta(seconds=1)).isoformat(),local_assets()[1])['status']==expected

@pytest.mark.parametrize('events,truncated,expected',[([],False,'inconclusive'),([{'event':'service_started'}],False,'inconclusive'),([{'event':'service_failed'}],False,'finding'),([{'event':'service_failed'}],True,'inconclusive')])
def test_C17_filtered_logs(events,truncated,expected):
    now,e=evidence();e.update(complete=False,truncated=truncated)
    assert evaluate('docker.get_container_logs',{'events':events,'complete':False},e,now.isoformat(),local_assets()[1])['status']==expected

def test_C17_summary_never_projects_model_prose_or_unknown_fields():
    report={'status':'succeeded','inspection_report':report_for([],datetime.now(timezone.utc).isoformat()),'diagnosis':{'summary':'private free text'}}
    payload=licensed_summary(report)
    assert 'private free text' not in json.dumps(payload)
    payload['inspection']['items'][0]['reason']='unlicensed free text'
    with pytest.raises(ValueError):validate_payload(payload)

def test_C19_inspection_cannot_request_r2():
    with pytest.raises(ValidationError):inspection_task(operation='restart_drill_container',constraints={'max_risk_class':'R2'})

async def run_inspection(environment, task=None):
    cfg,store,worker,*_=environment
    t=task or inspection_task();tid,_=await store.create(t);row=await store.claim(tid,'sec-m3-fixture')
    worker.contexts[tid]={'jwt':'fixture-jwt','verification':None}
    await worker.run_task(row)
    return await store.report(tid,91001)

async def test_C16_real_db_fixed_inspection_and_report(environment):
    cfg,store,w,p,counts,*_=environment
    report=await run_inspection(environment)
    assert report['status']=='succeeded' and report['task_kind']=='security_inspection'
    assert counts['reads']==1 and counts['writes']==0
    assert report['inspection_report']['review_status']=='completed'
    assert any(i['status']=='passed' for i in report['inspection_report']['items'])
    assert any(i['status']=='not_checked' for i in report['inspection_report']['items'])
    assert [r['kind'] for r in report['security_reviews']]==['sensitive_read','inspection_report']
    assert all(r['policy_version']==INSPECTION_POLICY for r in report['security_reviews'])
    assert report['approvals']==report['execution_attempts']==[]
    with pytest.raises(HTTPException) as e:await store.report(__import__('uuid').UUID(report['task_id']),999)
    assert e.value.status_code==404

@pytest.mark.parametrize('decision',['deny','needs_clarification'])
async def test_C19_opinion_does_not_change_collected_facts(environment,decision):
    _,store,w,p,counts,*_=environment;p.decisions['inspection_report']=decision
    report=await run_inspection(environment)
    assert report['status']=='succeeded' and counts['reads']==1 and counts['writes']==0
    assert report['inspection_report']['hypotheses'][0]['decision']==decision
    assert any(i['status']=='passed' for i in report['inspection_report']['items'])

async def test_C18_model_outage_retains_facts(environment):
    _,store,w,p,counts,*_=environment;p.fail_kind='inspection_report'
    report=await run_inspection(environment)
    assert report['status']=='failed' and counts['reads']==1
    assert report['inspection_report']['review_status']=='unavailable'
    assert any(i['status']=='passed' for i in report['inspection_report']['items'])
    assert report['security_reviews'][-1]['state']=='unavailable'

async def test_C18_cancel_after_read_keeps_evidence_zero_followup(environment):
    _,store,w,p,counts,*_=environment;counts['cancel_after_read']=True
    report=await run_inspection(environment)
    assert report['status']=='cancelled' and counts['reads']==1
    assert len(report['security_reviews'])==1 and report['infrastructure_evidence']
    assert any(i['status']=='passed' for i in report['inspection_report']['items'])

async def test_C15_ambiguous_zero_collector(environment):
    _,store,w,p,counts,*_=environment
    report=await run_inspection(environment,inspection_task(user_request='安全巡检服务器'))
    assert report['status']=='failed' and report['error']['code']=='target_ambiguous'
    assert counts['reads']==0 and p.calls==[]

async def test_C18_denied_read_zero_collector(environment):
    _,store,w,p,counts,*_=environment;p.decisions['sensitive_read']='deny'
    report=await run_inspection(environment)
    assert report['status']=='failed' and counts['reads']==0
    assert report['inspection_report']['unchecked_items']

async def test_C18_idempotency_kind_and_restart(environment):
    _,store,w,p,counts,*_=environment
    t=inspection_task();tid,new=await store.create(t)
    assert new and (await store.create(t))==(tid,False)
    altered=t.model_copy(update={'task_kind':'ops'})
    with pytest.raises(HTTPException) as e:await store.create(altered)
    assert e.value.status_code==409
    row=await store.claim(tid,'fixture-interrupted')
    await store.recover(startup=True)
    report=await store.report(tid,91001)
    assert report['status']=='failed' and counts['reads']==0 and p.calls==[]
    assert await store.claim(tid,'fixture-replay') is None

async def test_C17_egress_bound_to_report_and_assets(environment):
    _,store,w,p,counts,*_=environment
    report=await run_inspection(environment);tid=__import__('uuid').UUID(report['task_id']);row=await store.get(tid,91001)
    step=await store.pool.fetchrow('SELECT * FROM ma_task_steps WHERE task_id=$1 AND asset_id IS NOT NULL ORDER BY step_no DESC LIMIT 1',tid)
    payload=licensed_summary(report)
    rid=await store.security_gates.require(w.executor,row,row['execution_epoch'],step['step_no'],step['tool'],json.loads(step['args']),'fixture-jwt',row['result'],'data_egress',payload=payload,receiver='supervisor',history=True)
    assert await store.security_gates.check_egress(tid,rid,91001,digest(payload),'supervisor')
    with pytest.raises(ValueError):await store.security_gates.check_egress(tid,rid,91001,'a'*64,'supervisor')
    await store.pool.execute('UPDATE ma_assets SET version=2 WHERE asset_id=$1','fixture-drill')
    with pytest.raises(ValueError):await store.security_gates.check_egress(tid,rid,91001,digest(payload),'supervisor')

def test_C16_full_fixed_tool_checklist_lists_each_missing_capability():
    registry=local_assets();registry[1]['capabilities']=['docker.inspect_container']
    report=report_for(registry,datetime.now(timezone.utc).isoformat())
    from ops_contract import TOOLS
    expected={a['asset_id']+':'+tool for a in registry for tool,(kind,_) in TOOLS.items() if kind==a['kind'] and tool not in a['capabilities']}
    assert expected <= {i['check_id'] for i in report['unchecked_items']}
    assert len(make_plan(registry,16))==1

@pytest.mark.parametrize('tool,data,expected', [
    ('linux.list_services',{'list':[{'ActiveState':'failed','LoadState':'loaded'}]},'finding'),
    ('linux.list_services',{'list':[{'ActiveState':'active','LoadState':'loaded'}]},'passed'),
    ('linux.list_services',{'list':[]},'inconclusive'),
    ('postgresql.get_db_status',{'reachable':False},'finding'),
    ('postgresql.get_db_status',{'reachable':True},'passed'),
    ('linux.get_cpu_usage',{'cpu_percent':99.9},'inconclusive'),
    ('postgresql.get_connection_stats',{'waiting':999},'inconclusive')])
def test_C17_service_database_rules_and_no_resource_threshold(tool,data,expected):
    now,e=evidence();data['complete']=True
    assert evaluate(tool,data,e,now.isoformat(),local_assets()[1])['status']==expected

def test_C16_plan_covers_registered_eleven_r0_without_writes():
    from ops_contract import TOOLS
    registry=assets()
    for a in registry:a['capabilities']=[t for t,(kind,_) in TOOLS.items() if kind==a['kind']]
    tools=[t for _,t in make_plan(registry,16)]
    assert set(tools)==set(TOOLS) and 'docker.restart_drill_container' not in tools


def test_C15_original_target_survives_supervisor_whitespace_rephrasing():
    registry=local_assets();registry[1]['display_name']='本机SEC-M3隔离演练容器'
    assert select_assets(registry,{'user_request':'请巡检本机SEC-M3隔离演练容器','targets':[{'description':'本机 SEC-M3 隔离演练容器'}]})[0]['asset_id']=='fixture-drill'
    with pytest.raises(ValueError,match='target_ambiguous'):
        select_assets(registry,{'user_request':'巡检 fixture-drill','targets':[{'description':'fixture-host'}]})
