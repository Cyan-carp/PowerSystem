"""Explicit M4 deployment boundaries; legacy acceptance is never upgraded."""
import json
import hashlib
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlsplit

M4_POLICY = 'sec-m4-policy/v2'
PROFILES = {
    'm4-local': ('sec-m4-local-integration/v2', ('acceptance', 'fixture')),
    'm4-server-readonly': ('sec-m4-project-readonly/v6', ('production',)),
}

# Exact schema of the released v6 Config, verified against the production image.
# New action authority switches belong to its separately bound domain. Retaining
# this schema reproduces old v6 digests; it does not rewrite saved task contexts.
V6_CONTEXT_FIELDS = (
    'database_url', 'redis_url', 'service_token', 'actor_key', 'envelope_key',
    'backend_url', 'supervisor_url', 'knowledge_token', 'llm_url', 'model', 'llm_key',
    'enabled', 'write_enabled', 'infra_enabled', 'executor_url', 'executor_token',
    'executor_grant_key', 'executor_write_key', 'approval_timeout_seconds',
    'acceptance_drill_only', 'task_timeout', 'model_timeout', 'tool_timeout',
    'max_rounds', 'max_calls', 'max_tokens', 'tool_chars', 'global_concurrency',
    'user_active_limit', 'queue_capacity', 'lease_seconds', 'heartbeat_seconds',
    'verify_max_pages', 'telemetry_max_age_seconds', 'future_skew_seconds',
    'redis_prefix', 'audit_dir', 'runtime_profile', 'inspection_enabled',
    'security_enabled', 'security_url', 'security_service_token',
    'security_request_key', 'security_result_key',
)

def context(cfg):
    profile = getattr(cfg, 'runtime_profile', 'legacy')
    if profile == 'legacy': return None
    if profile not in PROFILES: raise ValueError('runtime_context_unavailable')
    # Persist only a digest. Credentials and private origins never enter the task DTO.
    names = V6_CONTEXT_FIELDS if profile == 'm4-server-readonly' else tuple(f.name for f in fields(cfg))
    material={name:str(getattr(cfg,name)) if isinstance(getattr(cfg,name),Path) else getattr(cfg,name) for name in names}
    fingerprint=hashlib.sha256(json.dumps(material,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return {'profile': profile, 'policy_version': 'sec-m4-policy/v6' if profile=='m4-server-readonly' else M4_POLICY, 'scope_version': PROFILES[profile][0], 'configuration_hash':fingerprint}

def check_context(task, cfg):
    raw = task.get('runtime_context')
    saved = json.loads(raw) if isinstance(raw, str) else raw
    expected = context(cfg)
    if saved != expected: raise ValueError('security_review_stale')
    return saved

def environments(cfg):
    profile = getattr(cfg, 'runtime_profile', 'legacy') if cfg else 'legacy'
    return PROFILES[profile][1] if profile in PROFILES else ('acceptance', 'fixture')

def validate(cfg):
    profile = cfg.runtime_profile
    db = urlsplit(cfg.database_url)
    if profile == 'legacy': return
    if profile == 'm4-local':
        if (db.hostname, db.port, db.path) not in (('127.0.0.1',15435,'/sec_m4_acceptance_test'),('127.0.0.1',15435,'/sec_m4_regression_test')):
            raise ValueError('isolated_m4_database_required')
        if (cfg.backend_url,cfg.supervisor_url,cfg.security_url,cfg.executor_url) != ('http://127.0.0.1:18106','http://127.0.0.1:18098','http://127.0.0.1:8114','http://127.0.0.1:8116'):
            raise ValueError('isolated_m4_origins_required')
        if not cfg.security_enabled or not cfg.infra_enabled: raise ValueError('m4_security_required')
    elif profile == 'm4-server-readonly':
        if (db.hostname, db.port, db.path) != ('postgres',5432,'/ma_ops'):
            raise ValueError('dedicated_ma_ops_database_required')
        if (cfg.backend_url,cfg.supervisor_url,cfg.security_url,cfg.executor_url) != ('http://api:8080','http://agent-rag:8093','http://security-agent:8100','http://ops-executor:8095'):
            raise ValueError('project_internal_origins_required')
        if not cfg.security_enabled or not cfg.infra_enabled or cfg.write_enabled or cfg.executor_write_key or cfg.acceptance_drill_only:
            raise ValueError('server_read_only_required')
        if cfg.model != 'deepseek-v4.1-flash' or cfg.llm_url != 'https://hgapi.dieqiyun.top/v1':
            raise ValueError('unlicensed_runtime_model')
    elif profile == 'm4-restore-readonly':
        if db.hostname not in ('127.0.0.1','postgres-restore') or not db.path.startswith('/ma_m4_restore_'):
            raise ValueError('isolated_restore_database_required')
        if cfg.enabled or cfg.write_enabled or cfg.infra_enabled or cfg.security_enabled or cfg.inspection_enabled or cfg.llm_key or cfg.executor_write_key or cfg.executor_token or cfg.executor_grant_key:
            raise ValueError('credential_free_restore_required')
        if not cfg.redis_prefix.startswith('ma:restore:'): raise ValueError('isolated_restore_namespace_required')
    else: raise ValueError('unknown_runtime_profile')
