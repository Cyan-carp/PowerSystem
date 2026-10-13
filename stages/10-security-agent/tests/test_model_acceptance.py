"""Protect service-only evidence labeling and program-owned association fields."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
import pytest

from review_contract import Decision, Opinion, POLICY, SCOPE, projection
from security_agent.main import create_app
from security_agent.provider import Provider
from test_contract_service import config, headers, request, FixtureProvider

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('sec_acceptance',SCRIPTS/'accept-local.py')
acceptance=importlib.util.module_from_spec(spec);spec.loader.exec_module(acceptance)
from samples import CASES,standalone_request


@pytest.mark.parametrize('case',CASES,ids=lambda case:case['id'])
def test_fixed_samples_use_explicit_current_scope(case):
    req=standalone_request(case)
    assert req.scope_version==SCOPE and req.policy_version==POLICY
    assert set(e.source for e in req.evidence)=={'synthetic'}
    projected=projection(req)
    assert 'review_id' not in projected and 'user_id' not in json.dumps(projected)
    if case['id']=='clarify-target':assert req.asset is None
    else:assert req.asset is not None


@pytest.mark.parametrize('field',['review_id','protocol','user_id','signature','approved'])
async def test_model_cannot_generate_association_or_authority(field):
    cfg=config();req=request()
    class Injection(FixtureProvider):
        async def review(self,*args):
            result,usage=await super().review(*args);result[field]=str(uuid4());return result,usage
    app=create_app(cfg,Injection())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://sec') as client:
        response=await client.post('/internal/ma-review/v1/reviews',json=req.model_dump(mode='json'),headers=headers(req,cfg))
    assert response.status_code==422 and response.json()=={'detail':'model_invalid_decision'}


async def test_service_only_suite_never_signs_off_database_or_milestone(tmp_path,monkeypatch):
    run=tmp_path/'artifacts/stage10-security-agent/sec-m1-fixture';private=run/'private';private.mkdir(parents=True)
    for name,value in [('sec-service-token','S'*40),('sec-request-key','Q'*40),('sec-result-key','R'*40)]:
        (private/name).write_text(value)
    monkeypatch.setattr(acceptance,'ROOT',tmp_path)
    monkeypatch.setattr(acceptance,'manifest',lambda:{'fixture-source':'unchanged'})
    class FakeClient:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,*args,**kwargs):return httpx.Response(200,json={'enabled':True,'model':'gpt-5.6-sol'})
    monkeypatch.setattr(acceptance.httpx,'AsyncClient',lambda **kwargs:FakeClient())
    expected={case['intent']:case['expected'] for case in CASES}
    async def review(client,req,*args):
        opinion={'decision':expected[req.normalized_intent],'reason':'合成fixture意见','rule_hits':['SEC-R01'],'evidence_refs':['synthetic-e1']}
        decision=Decision(protocol='ma-review/v1',review_id=req.review_id,**opinion)
        return {'state':'completed','decision':decision.model_dump(mode='json'),'error_code':None,'usage':{'total_tokens':1},'duration_ms':1},True
    monkeypatch.setattr(acceptance,'review_service',review)
    args=argparse.Namespace(run=str(run),output=str(run/'service-fixture'),service_only=True,model='gpt-5.6-sol')
    assert await acceptance.main(args)==0
    summary=json.loads((run/'service-fixture/summary.json').read_text())
    assert summary['sample_suite_passed'] and summary['passed']==10
    assert summary['database_verified'] is False and summary['signed_off'] is False


@pytest.mark.parametrize('model',['gpt-5.6-sol','deepseek-v4-flash-0731'])
async def test_provider_enforces_json_mode_and_model_opinion_schema(model):
    cfg=config();cfg.model=model;captured=[]
    def respond(req):
        captured.append(json.loads(req.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'decision':'allow','reason':'合成意见','rule_hits':['SEC-R01'],'evidence_refs':['e1']})}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        value,_=await Provider(cfg,client).review(projection(request()),30)
    assert Opinion.model_validate(value).decision=='allow'
    assert captured[0]['model']==model
    if model=='gpt-5.6-sol':
        assert captured[0]['reasoning_effort']=='none'
        output=captured[0]['response_format']
        assert output['type']=='json_schema' and output['json_schema']['strict'] is True
        assert output['json_schema']['schema']['additionalProperties'] is False
        assert set(output['json_schema']['schema']['properties'])=={'decision','reason','rule_hits','evidence_refs'}
    else:assert 'reasoning_effort' not in captured[0]
    if model!='gpt-5.6-sol':assert captured[0]['response_format']=={'type':'json_object'}
    assert captured[0]['temperature']==0
    assert 'tools' not in captured[0]
    assert 'review_id' not in captured[0]['messages'][1]['content']
