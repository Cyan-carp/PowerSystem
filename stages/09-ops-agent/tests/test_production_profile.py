"""Legacy v6 digest stability and independent new authority configuration binding."""
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from ops_agent.config import Config
from ops_agent import production_profile as profile
from ops_agent.runtime_profile import context,V6_CONTEXT_FIELDS
from ops_agent.business_actions import BusinessAuthority
from ops_agent.schedule_actions import ScheduleAuthority


@pytest.fixture
def production(tmp_path,monkeypatch):
    raw=b'0123456789abcdef0123456789abcdef\n';dmi='synthetic-owned-vm-uuid'
    doc={'profile':profile.PROFILE,'host':{'hostname':'powersystem-prod','vm_id':117,
          'machine_id_sha256':hashlib.sha256(raw).hexdigest(),'dmi_uuid':dmi},
         'databases':{'business':'powersystem','rag':'powersystem_agent_rag','ops':'ma_ops'},
         'origins':{'api':'http://api:8080','rag':'http://agent-rag:8093','ops':'http://ops-agent:8094','security':'http://security-agent:8100'},
         'capabilities':{'actions':True,'schedules':True,'ops_write':False},
         'policies':{'business':profile.BUSINESS_POLICY,'schedule':profile.SCHEDULE_POLICY}}
    (tmp_path/'identity.json').write_text(json.dumps(doc));(tmp_path/'machine-id').write_bytes(raw)
    (tmp_path/'product-uuid').write_text(dmi)
    monkeypatch.setenv('V4_PRODUCTION_PROFILE',profile.PROFILE);monkeypatch.setattr(profile,'IDENTITY_ROOT',tmp_path)
    cfg=Config(database_url='postgresql://synthetic@postgres:5432/ma_ops',redis_url='redis://synthetic',
               service_token='a'*40,actor_key='b'*40,envelope_key='c'*40,
               backend_url='http://api:8080',supervisor_url='http://agent-rag:8093',
               security_url='http://security-agent:8100',executor_url='http://ops-executor:8095',
               runtime_profile='m4-server-readonly',model='deepseek-v4.1-flash',
               enabled=True,infra_enabled=True,security_enabled=True,inspection_enabled=True,
               executor_token='d'*40,executor_grant_key='e'*40,security_service_token='f'*40,
               security_request_key='g'*40,security_result_key='h'*40,
               v4_actions_enabled=True,v4_schedules_enabled=True)
    return cfg,tmp_path,doc


def test_v6_digest_is_exact_released_44_field_schema(production):
    cfg,_,_=production
    assert len(V6_CONTEXT_FIELDS)==44
    material={k:str(getattr(cfg,k)) if isinstance(getattr(cfg,k),Path) else getattr(cfg,k) for k in V6_CONTEXT_FIELDS}
    expected=hashlib.sha256(json.dumps(material,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    assert context(cfg)['configuration_hash']==expected
    assert context(replace(cfg,v4_actions_enabled=False,v4_schedules_enabled=False))==context(cfg)
    assert context(replace(cfg,redis_prefix='changed:'))!=context(cfg)


@pytest.mark.parametrize('mutation',[
    {'database_url':'postgresql://synthetic@postgres:5432/powersystem'},
    {'database_url':'postgresql://synthetic@127.0.0.1:5432/ma_ops'},
    {'backend_url':'http://other:8080'}, {'v4_actions_enabled':False},
    {'v4_schedules_enabled':False}, {'v4_ops_enabled':True},
    {'v4_joint_profile':'v4-s09-local'}, {'write_enabled':True},
    {'executor_write_key':'x'*40}, {'security_enabled':False},
])
def test_wrong_production_configuration_still_fails(production,mutation):
    cfg,_,_=production
    with pytest.raises(ValueError):profile.validate(replace(cfg,**mutation))


def test_original_v6_runtime_and_new_domain_are_independently_bound(production):
    cfg,_,_=production;cfg.validate()
    authority=BusinessAuthority(SimpleNamespace(state=SimpleNamespace(cfg=cfg)),None)
    bound=authority.bind_domain({'policy_version':'v4-s06-local/v1'})
    assert bound['policy_version']==profile.BUSINESS_POLICY
    assert bound['production_configuration']==profile.binding(cfg)
    authority.validate_domain(bound)
    cfg.actor_key='z'*40
    with pytest.raises(Exception) as err:authority.validate_domain(bound)
    assert err.value.detail=='production_configuration_changed'


def test_identity_drift_is_rejected_and_s07_is_not_in_capabilities(production):
    cfg,root,doc=production
    profile.validate(cfg)
    changed=copy.deepcopy(doc);changed['capabilities']['ops_write']=True
    (root/'identity.json').write_text(json.dumps(changed))
    with pytest.raises(ValueError):profile.validate(cfg)
    (root/'identity.json').write_text(json.dumps(doc));(root/'machine-id').write_bytes(b'another-vm\n')
    with pytest.raises(ValueError):profile.validate(cfg)


@pytest.mark.asyncio
async def test_guard_rechecks_actual_ops_database(production):
    cfg,_,_=production
    class Pool:
        async def fetchval(self,query):return 'wrong_database'
    for cls in (BusinessAuthority,ScheduleAuthority):
        svc=cls(SimpleNamespace(state=SimpleNamespace(cfg=cfg,store=SimpleNamespace(pool=Pool()))),None)
        with pytest.raises(Exception) as err:await svc.guard()
        assert err.value.status_code==403
