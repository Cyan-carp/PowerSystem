from datetime import datetime,timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from rag_agent.legacy_run_read import validate,envelope,check_report,PROTOCOL

def test_explicit_bridge_stable_binding_and_r0_only():
    row=dict(id=uuid4(),session_id=uuid4(),user_id=3,turn_id=12,created_at=datetime.now(timezone.utc))
    key=uuid4();body=dict(session_id=str(row['session_id']),actor=dict(user_id=3),operation='read_only',constraints=dict(max_risk_class='R0'),idempotency_key=str(key))
    a=envelope(row,key,body);b=envelope(row,key,body)
    assert a==b and a['parent_turn_id']==12 and a['protocol']=='ma/v1' and 'run_id' not in a
    assert a['task_id']!=envelope({**row,'id':uuid4()},key,body)['task_id']
    assert a['task_id']!=envelope(row,key,body,'verifications')['task_id']
    assert a['idempotency_key']==body['idempotency_key']
    with pytest.raises(HTTPException):envelope(row,None,body)
    with pytest.raises(HTTPException):envelope(row,key,{**body,'operation':'restart_drill_container'})
    with pytest.raises(ValueError):validate(SimpleNamespace(v4_read_protocol=PROTOCOL,v4_actions_enabled=True))
    validate(SimpleNamespace(v4_read_protocol=PROTOCOL))

def test_child_report_binding_never_silently_falls_back_or_crosses_turn():
    row=dict(session_id=uuid4(),turn_id=12,user_id=3);tid=uuid4()
    report=dict(protocol='ma/v1',session_id=str(row['session_id']),parent_turn_id=12,task_id=str(tid))
    assert check_report(row,report,tid)==report
    for changed in ({'parent_turn_id':13},{'session_id':str(uuid4())},{'task_id':str(uuid4())},{'protocol':'ma-run/v1'},{'actor':{'user_id':4}}):
        with pytest.raises(HTTPException):check_report(row,{**report,**changed},tid)
