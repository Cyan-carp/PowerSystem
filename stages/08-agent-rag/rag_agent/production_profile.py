"""Explicit VM-bound production topology, with a distinct schedule bridge identity."""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

PROFILE = 'v4-business-schedule/v1'
SCHEDULE_POLICY = 'v4-s08-production/v1'
IDENTITY_ROOT = Path('/run/v4-production')


def selected():
    return os.getenv('V4_PRODUCTION_PROFILE', '') == PROFILE


def identity():
    if not selected():raise ValueError('explicit_v4_production_profile_required')
    root=IDENTITY_ROOT
    doc=json.loads((root/'identity.json').read_text(encoding='utf-8'))
    if (set(doc)!={'profile','host','databases','origins','capabilities','policies'}
        or doc['profile']!=PROFILE
        or doc['databases']!={'business':'powersystem','rag':'powersystem_agent_rag','ops':'ma_ops'}
        or doc['origins']!={'api':'http://api:8080','rag':'http://agent-rag:8093','ops':'http://ops-agent:8094','security':'http://security-agent:8100'}
        or doc['capabilities']!={'actions':True,'schedules':True,'ops_write':False}
        or doc['policies']!={'business':'v4-s06-production/v1','schedule':SCHEDULE_POLICY}):
        raise ValueError('invalid_v4_production_identity')
    h=doc['host']
    if (set(h)!={'hostname','vm_id','machine_id_sha256','dmi_uuid'}
        or h['hostname']!='powersystem-prod' or type(h['vm_id']) is not int or h['vm_id']!=117
        or hashlib.sha256((root/'machine-id').read_bytes()).hexdigest()!=h['machine_id_sha256']
        or (root/'product-uuid').read_text().strip().lower()!=h['dmi_uuid']):
        raise ValueError('v4_production_host_identity_changed')
    return doc


def validate(cfg):
    doc=identity();p=urlsplit(cfg.database_url)
    if (p.scheme not in ('postgres','postgresql') or p.hostname!='postgres'
        or (p.port or 5432)!=5432 or p.path!='/powersystem_agent_rag' or p.query or p.fragment
        or cfg.backend_url!='http://api:8080' or cfg.ops_url!='http://ops-agent:8094'
        or cfg.v4_joint_profile or cfg.v4_ops_enabled
        or not all((cfg.v4_actions_enabled,cfg.v4_schedules_enabled,cfg.v4_assistant_enabled,cfg.v4_runs_enabled,cfg.v4_artifacts_enabled))
        or cfg.v4_read_protocol!='ma-run/v1' or cfg.tool_timeout<40
        or len(cfg.schedule_bridge_token)<32 or cfg.schedule_bridge_token in (cfg.service_token,cfg.ops_token,cfg.ma_actor_key,cfg.ma_envelope_key)):
        raise ValueError('invalid_v4_production_rag_configuration')
    return doc


async def guard(cfg,pool):
    validate(cfg)
    if await pool.fetchval('SELECT current_database()')!='powersystem_agent_rag':
        raise ValueError('v4_production_database_identity_changed')
