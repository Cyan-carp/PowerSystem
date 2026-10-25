"""Explicit R0 bridge for an unchanged ma/v1 production child authority.

The RAG journal owns run-to-child binding; the old service still owns its
signed actor/envelope, current identity, idempotency and security gates.
No fallback, write conversion, approval, sensitive egress or replay on restore.
"""
from uuid import UUID,uuid5
from fastapi import HTTPException

PROTOCOL='ma/v1-bound-read'

def validate(cfg):
    value=getattr(cfg,'v4_read_protocol','ma-run/v1')
    if value not in ('ma-run/v1',PROTOCOL):raise ValueError('invalid_v4_read_protocol')
    if value==PROTOCOL and any(getattr(cfg,k,False) for k in ('v4_actions_enabled','v4_ops_enabled','v4_schedules_enabled','v4_joint_profile')):
        raise ValueError('legacy_run_bridge_read_only_required')

def envelope(row,dispatch_key,body,kind='tasks'):
    if body.get('operation','read_only')!='read_only' or body.get('constraints',{}).get('max_risk_class')!='R0':
        raise HTTPException(403,'legacy_run_bridge_read_only_required')
    if body.get('session_id')!=str(row['session_id']) or body.get('actor',{}).get('user_id')!=row['user_id']:
        raise HTTPException(403,'legacy_run_binding_changed')
    if dispatch_key is None or body.get('idempotency_key')!=str(dispatch_key):raise HTTPException(403,'legacy_run_binding_changed')
    if kind not in ('tasks','verifications'):raise HTTPException(403,'legacy_run_binding_changed')
    return {**body,'protocol':'ma/v1','task_id':str(uuid5(row['id'],'legacy-'+kind+':'+str(dispatch_key))),
            'parent_turn_id':row['turn_id'],'created_at':row['created_at'].isoformat()}

def check_report(row,value,expected_task=None):
    if not isinstance(value,dict) or value.get('protocol')!='ma/v1' or value.get('session_id')!=str(row['session_id']) or value.get('parent_turn_id')!=row['turn_id']:
        raise HTTPException(409,'legacy_run_binding_changed')
    if expected_task is not None and value.get('task_id')!=str(expected_task):raise HTTPException(409,'legacy_run_binding_changed')
    if value.get('actor') is not None and value['actor'].get('user_id')!=row.get('user_id'):raise HTTPException(409,'legacy_run_binding_changed')
    # Legacy task reports do not claim a run_id. Never relabel their protocol.
    return value
