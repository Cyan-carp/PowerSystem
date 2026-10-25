"""S06 restricted canonical domain, hard boundaries and typed proposal tests."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from ops_agent.business_contract import canonical,digest,arguments,Proposal,binding,sign_grant,verify_grant
from pydantic import ValidationError
from uuid import uuid4
import time

def test_unicode_key_order_is_rfc8785_utf16_and_unsupported_values_fail():
    assert canonical({'\ue000':1,'\U0001f600':2})=='{"😀":2,"\ue000":1}'.encode()
    for value in (float('nan'),1.1,9007199254740992):
        with pytest.raises(ValueError):canonical({'value':value})

def test_unknown_argument_and_alternate_remark_name_are_rejected():
    for value in ({'device_id':1,'expected_version':0,'new_remark':'text'}, {'device_id':1,'expected_version':0,'remark':'text','role':'admin'}, {'device_id':True,'expected_version':0,'remark':'text'}):
        with pytest.raises(ValidationError):arguments('device.remark',value)

def test_each_frozen_binding_dimension_changes_plan_hash():
    proposal=Proposal(action_id=uuid4(),run_id=uuid4(),session_id=uuid4(),parent_turn_id=1,capability='device.remark',typed_args={},user_request='synthetic')
    value=binding(1,proposal,dict(device_id=1,remark='text',expected_version=0),'0','2026-10-24T00:00:00+00:00')
    for key,replacement in [('owner_id',2),('run_id',str(uuid4())),('action_id',str(uuid4())),('capability','alarm.ack'),('version','2.0'),('typed_args',dict(device_id=2,remark='text',expected_version=0)),('target_refs',[dict(kind='device',id=2)]),('target_version','1'),('data_class','sensitive'),('destination_id','external'),('destination_version','2'),('policy_version','another'),('expires_at','2026-10-25T00:00:00+00:00')]:
        assert digest(value)!=digest({**value,key:replacement}),key

def test_expired_and_changed_execution_grant_are_denied():
    key='synthetic-test-key-32-chars-only-000000';now=int(time.time())
    value=dict(aud='ma-business-execute/v1',iat=now,exp=now+60,owner_id=1)
    grant=sign_grant(value,key);assert verify_grant(grant,key)==value
    with pytest.raises(ValueError):verify_grant(grant[:-1]+'X',key)
    with pytest.raises(ValueError):verify_grant(sign_grant({**value,'exp':now-1},key),key)

@pytest.mark.asyncio
async def test_disabled_and_production_profiles_do_not_gain_business_writes():
    from types import SimpleNamespace
    from ops_agent.business_actions import BusinessAuthority
    from fastapi import HTTPException
    for cfg in (SimpleNamespace(v4_actions_enabled=False,database_url='postgresql://127.0.0.1:25436/v4_s06_unit_test'),SimpleNamespace(v4_actions_enabled=True,database_url='postgresql://postgres:5432/ma_ops',backend_url='http://api:8080',security_url='http://security-agent:8100',write_enabled=False,runtime_profile='m4-server-readonly')):
        app=SimpleNamespace(state=SimpleNamespace(cfg=cfg));svc=BusinessAuthority(app,None)
        with pytest.raises(HTTPException) as exc:await svc.guard()
        assert exc.value.status_code==403
