"""S09 local integration domain; never an upgrade of the stage or production profiles.

The three protocol/policy domains retain their original approval hashes. This
profile only co-locates their authorities on a pinned, synthetic test topology.
"""
import os
import re
from urllib.parse import urlsplit

PROFILE = 'v4-s09-local'
API = 'http://127.0.0.1:28992'
RAG = 'http://127.0.0.1:28991'
OPS = 'http://127.0.0.1:28973'
SEC = 'http://127.0.0.1:28974'
EXECUTOR = 'http://127.0.0.1:28975'

def database(url, role):
    try:
        p = urlsplit(url)
        return (p.scheme in ('postgres', 'postgresql') and p.hostname == '127.0.0.1'
                and p.port == 25439 and not p.query and not p.fragment
                and re.fullmatch(r'/v4_s09_r2_[a-z0-9_]+_' + role + r'_test', p.path) is not None)
    except (TypeError, ValueError):
        return False

def selected(cfg):
    return getattr(cfg, 'v4_joint_profile', '') == PROFILE

def validate(cfg, role):
    if not selected(cfg) or os.getenv('V4_S09_LOCAL_ENABLED') != 'true':
        raise ValueError('explicit_s09_local_profile_required')
    if role == 'rag' and (not database(cfg.database_url, role) or cfg.backend_url != API or cfg.ops_url != OPS):
        raise ValueError('isolated_s09_rag_topology_required')
    if role == 'ops':
        if (not database(cfg.database_url, 'ops') or
            (cfg.backend_url, cfg.supervisor_url, cfg.security_url, cfg.executor_url) != (API, RAG, SEC, EXECUTOR)
            or cfg.runtime_profile != 'legacy' or cfg.write_enabled or cfg.infra_enabled
            or cfg.inspection_enabled or cfg.security_enabled or cfg.acceptance_drill_only):
            raise ValueError('isolated_s09_ops_topology_required')
    if role not in ('rag', 'ops'):
        raise ValueError('invalid_s09_role')
    if not all((cfg.v4_actions_enabled, cfg.v4_ops_enabled, cfg.v4_schedules_enabled)):
        raise ValueError('complete_s09_capabilities_required')
    if role == 'rag' and not all((cfg.v4_assistant_enabled, cfg.v4_runs_enabled)):
        raise ValueError('complete_s09_runs_required')

async def guard(cfg, pool, role):
    validate(cfg, role)
    actual = await pool.fetchval('SELECT current_database()')
    if actual != urlsplit(cfg.database_url).path[1:]:
        raise ValueError('s09_database_identity_changed')
