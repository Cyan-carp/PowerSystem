import sys,time,asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ops_agent.service_contract import arguments,binding,Proposal,digest,verify_grant,sign_grant,POLICY
from ops_agent.service_actions import ServiceAuthority
from ops_contract import validate_catalog

def grant():
    at=int(time.time())
    return dict(aud='ma-ops-execute/v1',iat=at,exp=at+60,owner_id=1,run_id=str(uuid4()),action_id=str(uuid4()),execution_ref=str(uuid4()),capability='service.restart',typed_args={'asset_id':'s07-demo'},target_version='a'*64,plan_hash='b'*64,policy_version=POLICY)
KEY='synthetic-test-key-00000000000000000000000'
@pytest.mark.parametrize('args',[{'asset_id':'s07-demo','shell':'whoami'},{'asset_id':'http://localhost'},{'asset_id':'s07-demo','limit':True},{'asset_id':'s07-demo','limit':101}])
def test_unregistered_arguments_rejected(args):
    with pytest.raises(ValueError):arguments('service.logs',args)

def test_bound_domain_and_destination_are_fixed():
    p=Proposal(action_id=uuid4(),run_id=uuid4(),session_id=uuid4(),parent_turn_id=1,capability='service.restart',typed_args={'asset_id':'s07-demo'},user_request='重启 s07-demo')
    b=binding(1,p,p.typed_args,'a'*64,'2026-10-24T00:00:00Z')
    assert b['policy_version']==POLICY and b['destination_id']=='platform_client' and b['data_class']=='internal'
    baseline=digest(b)
    for key,value in [('owner_id',2),('run_id',str(uuid4())),('action_id',str(uuid4())),('target_version','b'*64),('destination_id','registered_model'),('policy_version','v4-s06-local/v1')]:assert digest({**b,key:value})!=baseline

@pytest.mark.parametrize('patch',[{'aud':'ma-business-execute/v1'},{'policy_version':'v4-s06-local/v1'},{'exp':0},{'typed_args':{'asset_id':'s07-demo','command':'restart'}},{'owner_id':True},{'capability':'service.delete'},{'extra':1}])
def test_invalid_even_authentic_grants_rejected(patch):
    with pytest.raises(ValueError):verify_grant(sign_grant({**grant(),**patch},KEY),KEY)

def test_grant_and_mac():
    g=grant();token=sign_grant(g,KEY);assert verify_grant(token,KEY)==g
    with pytest.raises(ValueError):verify_grant(token,KEY+'x')

def test_production_r0_does_not_gain_restart():
    common=dict(project_scope='powersystem',enabled=True,criticality='support',environment='production',version=1,verified_at='2026-10-24T00:00:00Z',connection_ref='prod',identity_hash='a'*64)
    host=dict(**common,asset_id='host',kind='host',display_name='host',parent_id=None,capabilities=[])
    svc=dict(**common,asset_id='svc',kind='service',display_name='svc',parent_id='host',capabilities=['service.restart'])
    with pytest.raises(ValueError):validate_catalog([host,svc])

def test_guard_rejects_s06_or_production_without_db_access():
    cfg=SimpleNamespace(v4_ops_enabled=True,database_url='postgresql://u@127.0.0.1:25436/v4_s06_test',backend_url='http://127.0.0.1:28972',security_url='http://127.0.0.1:28974',executor_url='http://127.0.0.1:28975',runtime_profile='legacy',write_enabled=False,v4_actions_enabled=False,executor_write_key=KEY)
    app=SimpleNamespace(state=SimpleNamespace(cfg=cfg))
    with pytest.raises(Exception) as exc:asyncio.run(ServiceAuthority(app,None).guard())
    assert exc.value.detail=='isolated_s07_policy_required'
