"""Explicit S09 synthetic integration profile; legacy stage guards stay separate."""
import os
import re
from urllib.parse import urlsplit

PROFILE = 'v4-s09-local'

def selected(cfg):
    return getattr(cfg, 'v4_joint_profile', '') == PROFILE

def validate(cfg, role='rag'):
    p = urlsplit(cfg.database_url)
    if (not selected(cfg) or os.getenv('V4_S09_LOCAL_ENABLED') != 'true' or role != 'rag'
        or p.scheme not in ('postgres','postgresql') or p.hostname != '127.0.0.1'
        or p.port != 25439 or p.query or p.fragment
        or not re.fullmatch(r'/v4_s09_r2_[a-z0-9_]+_rag_test',p.path)
        or cfg.backend_url != 'http://127.0.0.1:28992' or cfg.ops_url != 'http://127.0.0.1:28973'
        or not all((cfg.v4_actions_enabled,cfg.v4_ops_enabled,cfg.v4_schedules_enabled,
                    cfg.v4_assistant_enabled,cfg.v4_runs_enabled))):
        raise ValueError('isolated_s09_rag_topology_required')

async def guard(cfg,pool,role='rag'):
    validate(cfg,role)
    if await pool.fetchval('SELECT current_database()') != urlsplit(cfg.database_url).path[1:]:
        raise ValueError('s09_database_identity_changed')
