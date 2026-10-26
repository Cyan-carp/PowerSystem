"""Production schedules use distinct databases and never duplicate business identity."""
import hashlib,json,time
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from rag_agent.config import Config
from rag_agent import production_profile as profile
from rag_agent.schedules import ScheduleService,ScheduleConfig
from rag_agent.ma_security import digest,sign
from rag_agent.schedule_token import verify_action
from ops_agent.business_contract import sign_grant


@pytest.fixture
def production(tmp_path,monkeypatch):
    raw=b'owned-vm-machine-id\n';dmi='owned-vm-product-uuid'
    doc={'profile':profile.PROFILE,'host':{'hostname':'powersystem-prod','vm_id':117,
        'machine_id_sha256':hashlib.sha256(raw).hexdigest(),'dmi_uuid':dmi},
        'databases':{'business':'powersystem','rag':'powersystem_agent_rag','ops':'ma_ops'},
        'origins':{'api':'http://api:8080','rag':'http://agent-rag:8093','ops':'http://ops-agent:8094','security':'http://security-agent:8100'},
        'capabilities':{'actions':True,'schedules':True,'ops_write':False},
        'policies':{'business':'v4-s06-production/v1','schedule':profile.SCHEDULE_POLICY}}
    (tmp_path/'identity.json').write_text(json.dumps(doc));(tmp_path/'machine-id').write_bytes(raw)
    (tmp_path/'product-uuid').write_text(dmi)
    monkeypatch.setenv('V4_PRODUCTION_PROFILE',profile.PROFILE);monkeypatch.setattr(profile,'IDENTITY_ROOT',tmp_path)
    cfg=Config(model='deepseek-flash',llm_url='https://api.deepseek.com',llm_key='synthetic-key',
        service_token='a'*40,database_url='postgresql://synthetic@postgres:5432/powersystem_agent_rag',redis_url='redis://synthetic',
        backend_url='http://api:8080',ops_url='http://ops-agent:8094',ops_token='b'*40,
        ma_actor_key='c'*40,ma_envelope_key='d'*40,schedule_bridge_token='e'*40,
        v4_assistant_enabled=True,v4_runs_enabled=True,v4_artifacts_enabled=True,
        v4_actions_enabled=True,v4_schedules_enabled=True,v4_read_protocol='ma-run/v1',tool_timeout=45)
    return cfg


@pytest.mark.parametrize('mutation',[
    {'database_url':'postgresql://synthetic@postgres:5432/powersystem'},
    {'backend_url':'http://another:8080'}, {'ops_url':'http://127.0.0.1:28973'},
    {'v4_ops_enabled':True}, {'v4_joint_profile':'v4-s09-local'},
    {'schedule_bridge_token':'a'*40}, {'v4_read_protocol':'ma/v1-bound-read'}, {'tool_timeout':15},
])
def test_mixed_authority_or_wrong_topology_is_rejected(production,mutation):
    profile.validate(production)
    with pytest.raises(ValueError):profile.validate(replace(production,**mutation))


@pytest.mark.asyncio
async def test_delivery_lock_does_not_query_nonexistent_rag_users_table(production):
    sid,gid=uuid4(),uuid4();config=ScheduleConfig(task_type='inspection').model_dump();scope=digest(config)
    row={'id':sid,'user_id':7,'actor_role':'operator','status':'active','config':json.dumps(config),'config_version':1,'scope_hash':scope}
    grant={'schedule_id':sid,'user_id':7,'approved_by':7,'revoked_at':None,'expires_at':datetime.now(timezone.utc)+timedelta(days=1),
           'config_version':1,'scope_hash':scope,'destination_id':'platform_client','destination_version':'1'}
    class Pool:
        async def fetchval(self,query,*args):
            assert 'users' not in query
            return 'powersystem_agent_rag'
        async def fetchrow(self,query,*args):
            assert 'users' not in query
            return grant if 'assistant_schedule_grants' in query else row
    pool=Pool();app=SimpleNamespace(state=SimpleNamespace(config=production,runs=SimpleNamespace(pool=pool)))
    service=ScheduleService(app);await service.guard()
    assert (await service.valid(sid,gid,c=pool,lock=True))['config']==config
    grant['revoked_at']=datetime.now(timezone.utc)
    with pytest.raises(Exception) as err:await service.valid(sid,gid,c=pool,lock=True)
    assert err.value.status_code==409


@pytest.mark.asyncio
async def test_current_business_identity_rejects_forged_owner_and_revoked_role(production):
    at=int(time.time());actor={'aud':'ma-ops','kid':'ma-v1','iat':at,'exp':at+60,'user_id':7,'role':'operator'}
    class Pool:
        async def fetchval(self,query):return 'powersystem_agent_rag'
    class Client:
        async def get(self,url,**kwargs):
            assert url=='http://api:8080/internal/ma/v1/actor'
            assert kwargs['headers']['X-PowerSystem-Token']==production.ops_token
            return httpx.Response(200,json={'data':{'context':sign(actor,production.ma_actor_key)}})
    app=SimpleNamespace(state=SimpleNamespace(config=production,runs=SimpleNamespace(pool=Pool()),client=Client()))
    service=ScheduleService(app);await service.current_owner('synthetic-jwt',7)
    for owner,role in ((8,'operator'),(7,'viewer')):
        actor['role']=role
        with pytest.raises(Exception) as err:await service.current_owner('synthetic-jwt',owner)
        assert err.value.status_code==403


def test_schedule_dispatch_policy_domains_never_fallback(production):
    at=int(time.time());key='x'*40
    grant={'aud':'ma-schedule-execute/v1','iat':at,'exp':at+60,'owner_id':7,'run_id':str(uuid4()),
           'action_id':str(uuid4()),'execution_ref':str(uuid4()),'capability':'schedule.create',
           'typed_args':{'schedule_id':str(uuid4()),'expected_version':0,'config':ScheduleConfig(task_type='inspection').model_dump()},
           'target_version':'a'*64,'plan_hash':'b'*64,'policy_version':profile.SCHEDULE_POLICY}
    token=sign_grant(grant,key)
    assert verify_action(token,key,expected_policy=profile.SCHEDULE_POLICY)==grant
    with pytest.raises(ValueError):verify_action(token,key)
    with pytest.raises(ValueError):verify_action(token,key,expected_policy='arbitrary')
