"""Machine-bound business/schedule domain; legacy v6 remains a separate authority."""
import hashlib
import json
import os
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlsplit

PROFILE = 'v4-business-schedule/v1'
BUSINESS_POLICY = 'v4-s06-production/v1'
SCHEDULE_POLICY = 'v4-s08-production/v1'
IDENTITY_ROOT = Path('/run/v4-production')


def selected():
    return os.getenv('V4_PRODUCTION_PROFILE', '') == PROFILE


def identity():
    if not selected():
        raise ValueError('explicit_v4_production_profile_required')
    root = IDENTITY_ROOT
    doc = json.loads((root / 'identity.json').read_text(encoding='utf-8'))
    if (set(doc) != {'profile', 'host', 'databases', 'origins', 'capabilities', 'policies'}
        or doc['profile'] != PROFILE
        or doc['databases'] != {'business': 'powersystem', 'rag': 'powersystem_agent_rag', 'ops': 'ma_ops'}
        or doc['origins'] != {'api': 'http://api:8080', 'rag': 'http://agent-rag:8093',
                              'ops': 'http://ops-agent:8094', 'security': 'http://security-agent:8100'}
        or doc['capabilities'] != {'actions': True, 'schedules': True, 'ops_write': False}
        or doc['policies'] != {'business': BUSINESS_POLICY, 'schedule': SCHEDULE_POLICY}):
        raise ValueError('invalid_v4_production_identity')
    host = doc['host']
    if (set(host) != {'hostname', 'vm_id', 'machine_id_sha256', 'dmi_uuid'}
        or host['hostname'] != 'powersystem-prod' or type(host['vm_id']) is not int or host['vm_id'] != 117
        or hashlib.sha256((root / 'machine-id').read_bytes()).hexdigest() != host['machine_id_sha256']
        or (root / 'product-uuid').read_text().strip().lower() != host['dmi_uuid']):
        raise ValueError('v4_production_host_identity_changed')
    return doc


def validate(cfg):
    doc = identity()
    p = urlsplit(cfg.database_url)
    if (p.scheme not in ('postgres', 'postgresql') or p.hostname != 'postgres'
        or (p.port or 5432) != 5432 or p.path != '/ma_ops' or p.query or p.fragment
        or cfg.runtime_profile != 'm4-server-readonly'
        or (cfg.backend_url, cfg.supervisor_url, cfg.security_url, cfg.executor_url) !=
           ('http://api:8080', 'http://agent-rag:8093', 'http://security-agent:8100', 'http://ops-executor:8095')
        or not cfg.v4_actions_enabled or not cfg.v4_schedules_enabled or cfg.v4_ops_enabled
        or cfg.v4_joint_profile or cfg.write_enabled or cfg.executor_write_key or cfg.acceptance_drill_only
        or not cfg.infra_enabled or not cfg.security_enabled or not cfg.inspection_enabled):
        raise ValueError('invalid_v4_production_ops_configuration')
    return doc


async def guard(cfg, pool):
    validate(cfg)
    if await pool.fetchval('SELECT current_database()') != 'ma_ops':
        raise ValueError('v4_production_database_identity_changed')


def binding(cfg):
    """New approvals bind the full effective domain, including secret digests."""
    doc = validate(cfg)
    material = {f.name: str(getattr(cfg, f.name)) if isinstance(getattr(cfg, f.name), Path)
                else getattr(cfg, f.name) for f in fields(cfg)}
    return hashlib.sha256(json.dumps({'identity': doc, 'configuration': material},
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()
