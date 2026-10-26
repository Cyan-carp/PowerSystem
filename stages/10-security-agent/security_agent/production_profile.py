"""Explicit V4 production profile shared by the S06 and S08 review domains.

The production profile is layered on top of the existing ``m4-server-readonly``
runtime profile: the legacy ``ma-review/v1`` domain keeps its v6 model, URL and
key mapping untouched, while the two new internal domains use their own official
DeepSeek endpoint, credential and policy versions. Local and joint profiles, and
the S07 ops domain, keep their original routing. A profile that is not explicitly
configured never reaches the new credential.
"""

V4_PRODUCTION_PROFILE = 'v4-business-schedule/v1'

ACTION_LOCAL_POLICY = 'v4-s06-local/v1'
ACTION_PRODUCTION_POLICY = 'v4-s06-production/v1'
SCHEDULE_LOCAL_POLICY = 'v4-s08-local/v1'
SCHEDULE_PRODUCTION_POLICY = 'v4-s08-production/v1'


def production(cfg):
    """True only for the exact, explicitly configured production profile."""
    return getattr(cfg, 'production_profile', '') == V4_PRODUCTION_PROFILE


def action_domain(cfg):
    """Return ``(mode, policy_version)`` for S06, or ``None`` when disabled."""
    if production(cfg):
        return 'production', ACTION_PRODUCTION_POLICY
    if getattr(cfg, 'runtime_profile', '') in ('v4-s06-local', 'v4-s09-local'):
        return 'local', ACTION_LOCAL_POLICY
    return None


def schedule_domain(cfg):
    """Return ``(mode, policy_version)`` for S08, or ``None`` when disabled."""
    if production(cfg):
        return 'production', SCHEDULE_PRODUCTION_POLICY
    if getattr(cfg, 'runtime_profile', '') in ('v4-s08-local', 'v4-s09-local'):
        return 'local', SCHEDULE_LOCAL_POLICY
    return None


def model_access(cfg, mode):
    """Select the destination/model/key pair for the resolved domain mode.

    The production pair is a separate credential; the local pair is the legacy
    candidate configuration. No implicit fallback between the two exists.
    """
    if mode == 'production':
        return {'url': cfg.action_llm_url, 'key': cfg.action_llm_key, 'model': cfg.action_model}
    return {'url': cfg.llm_url, 'key': cfg.llm_key, 'model': 'deepseek-flash'}
