import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def secret(name):
    path = os.getenv(name, "")
    return Path(path).read_text(encoding="utf-8-sig").strip() if path else ""


@dataclass
class Config:
    database_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    service_token: str = field(repr=False)
    actor_key: str = field(repr=False)
    envelope_key: str = field(repr=False)
    backend_url: str = "http://127.0.0.1:8080"
    supervisor_url: str = "http://127.0.0.1:8093"
    knowledge_token: str = field(default="", repr=False)
    llm_url: str = "https://hgapi.dieqiyun.top/v1"
    model: str = "deepseek-v4-flash-0731"
    llm_key: str = field(default="", repr=False)
    enabled: bool = False
    write_enabled: bool = False
    infra_enabled: bool = False
    executor_url: str = 'http://127.0.0.1:8095'
    executor_token: str = field(default='', repr=False)
    executor_grant_key: str = field(default='', repr=False)
    executor_write_key: str = field(default='', repr=False)
    approval_timeout_seconds: int = 1800
    acceptance_drill_only: bool = False
    task_timeout: int = 300
    model_timeout: int = 60
    tool_timeout: int = 15
    max_rounds: int = 8
    max_calls: int = 16
    max_tokens: int = 8192
    tool_chars: int = 60000
    global_concurrency: int = 2
    user_active_limit: int = 1
    queue_capacity: int = 20
    lease_seconds: int = 50
    heartbeat_seconds: int = 10
    verify_max_pages: int = 10
    telemetry_max_age_seconds: float = 120
    future_skew_seconds: float = 60
    redis_prefix: str = "ma:"
    audit_dir: Path = Path("artifacts/stage9-ops-agent/audit")
    runtime_profile: str = "legacy"
    inspection_enabled: bool = False
    security_enabled: bool = False
    security_url: str = 'http://127.0.0.1:8100'
    security_service_token: str = field(default='', repr=False)
    security_request_key: str = field(default='', repr=False)
    security_result_key: str = field(default='', repr=False)
    v4_joint_profile: str = ''
    v4_actions_enabled: bool = False
    v4_ops_enabled: bool = False
    v4_schedules_enabled: bool = False

    @classmethod
    def load(cls):
        cfg = cls(database_url=os.environ["OPS_DATABASE_URL"], redis_url=os.environ["OPS_REDIS_URL"],
                  service_token=secret("OPS_SERVICE_TOKEN_FILE"), actor_key=secret("MA_ACTOR_KEY_FILE"),
                  envelope_key=secret("MA_ENVELOPE_KEY_FILE"), llm_key=secret("OPS_LLM_API_KEY_FILE"),
                  knowledge_token=secret("AGENT_SERVICE_TOKEN_FILE"),
                  executor_token=secret('OPS_EXECUTOR_TOKEN_FILE'),executor_grant_key=secret('OPS_EXECUTOR_GRANT_KEY_FILE'))
        cfg.executor_write_key=secret('OPS_EXECUTOR_WRITE_KEY_FILE')
        cfg.v4_actions_enabled=os.getenv('V4_ACTIONS_ENABLED','false').lower()=='true'
        ops_switch=os.getenv('V4_OPS_WRITE_ENABLED','false').lower()
        if ops_switch not in ('true','false'):raise ValueError('invalid_v4_ops_switch')
        cfg.v4_ops_enabled=ops_switch=='true'
        schedule_switch=os.getenv('V4_SCHEDULES_ENABLED','false').lower()
        if schedule_switch not in ('true','false'):raise ValueError('invalid_v4_schedules_switch')
        cfg.v4_schedules_enabled=schedule_switch=='true'
        if cfg.v4_actions_enabled or cfg.v4_ops_enabled or cfg.v4_schedules_enabled:
            cfg.security_service_token=secret('SEC_SERVICE_TOKEN_FILE')
            cfg.security_request_key=secret('SEC_REQUEST_KEY_FILE')
            cfg.security_result_key=secret('SEC_RESULT_KEY_FILE')
        raw_inspection = os.getenv('OPS_INSPECTION_ENABLED','false').lower()
        if raw_inspection not in ('true','false'): raise ValueError('invalid_inspection_switch')
        cfg.inspection_enabled = raw_inspection == 'true'
        raw_security = os.getenv('OPS_SECURITY_ENABLED', 'false').lower()
        if raw_security not in ('true', 'false'): raise ValueError('invalid_security_switch')
        cfg.security_enabled = raw_security == 'true'
        cfg.security_url = os.getenv('OPS_SECURITY_URL', cfg.security_url)
        if cfg.security_enabled:
            cfg.security_service_token = secret('SEC_SERVICE_TOKEN_FILE')
            cfg.security_request_key = secret('SEC_REQUEST_KEY_FILE')
            cfg.security_result_key = secret('SEC_RESULT_KEY_FILE')
        cfg.approval_timeout_seconds=int(os.getenv('OPS_APPROVAL_TIMEOUT_SECONDS','1800'))
        cfg.acceptance_drill_only=os.getenv('MA_DRILL_ACCEPTANCE_ONLY','false').lower()=='true'
        for field in ("backend_url", "supervisor_url", "llm_url", "model", "redis_prefix", 'executor_url'):
            setattr(cfg, field, os.getenv("OPS_" + field.upper(), getattr(cfg, field)).rstrip("/") if field.endswith("url") else os.getenv("OPS_" + field.upper(), getattr(cfg, field)))
        for field in ("enabled", "write_enabled", 'infra_enabled'):
            raw = os.getenv("OPS_" + field.upper(), "false").lower()
            if raw not in ("true", "false", "1", "0"): raise ValueError("invalid_ops_switch")
            setattr(cfg, field, raw in ("true", "1"))
        for field in ("task_timeout", "model_timeout", "tool_timeout", "max_rounds", "max_calls", "max_tokens", "tool_chars", "global_concurrency", "user_active_limit", "queue_capacity", "lease_seconds", "heartbeat_seconds", "verify_max_pages"):
            name = "OPS_" + field.upper() + ("_SECONDS" if field in ("task_timeout", "model_timeout", "tool_timeout") else "")
            setattr(cfg, field, int(os.getenv(name, getattr(cfg, field))))
        cfg.audit_dir = Path(os.getenv("OPS_AUDIT_DIR", str(cfg.audit_dir)))
        cfg.runtime_profile = os.getenv("OPS_RUNTIME_PROFILE", "legacy")
        cfg.v4_joint_profile=os.getenv('V4_JOINT_PROFILE','')
        cfg.validate()
        return cfg

    def validate(self):
        if self.v4_joint_profile:
            from v4_joint import validate as validate_joint
            validate_joint(self,'ops')
        from .runtime_profile import validate
        validate(self)
        m4 = self.runtime_profile in ("m4-local", "m4-server-readonly", "m4-restore-readonly")
        legacy_drill = self.database_url.endswith('/ma_m3_ops_test') and self.backend_url == 'http://127.0.0.1:18103'
        sec_drill = self.security_enabled and self.database_url.endswith('/sec_m2_acceptance_test') and self.backend_url == 'http://127.0.0.1:18104'
        sec_inspection = self.inspection_enabled and self.security_enabled and urlsplit(self.database_url).path in ('/sec_m3_acceptance_test','/sec_m3_regression_test') and self.backend_url=='http://127.0.0.1:18105' and self.security_url=='http://127.0.0.1:8110' and self.supervisor_url=='http://127.0.0.1:18097' and self.executor_url=='http://127.0.0.1:8112'
        if self.inspection_enabled and not m4 and (not sec_inspection or not self.infra_enabled or self.write_enabled):
            raise ValueError('isolated_read_only_inspection_required')
        if self.acceptance_drill_only and not (legacy_drill or sec_drill or sec_inspection or self.runtime_profile == "m4-local"):
            raise ValueError('dedicated_drill_acceptance_required')
        if self.security_enabled:
            if self.security_url not in ('http://127.0.0.1:8100', 'http://security-agent:8100') and not sec_inspection and not m4:
                raise ValueError('invalid_security_origin')
            values = (self.security_service_token, self.security_request_key, self.security_result_key)
            if min(map(len, values)) < 32 or len(set(values)) != 3:
                raise ValueError('security_private_credentials_required')
            if urlsplit(self.database_url).path not in ('/sec_m2_acceptance_test', '/sec_m2_regression_test') and not sec_inspection and not m4:
                raise ValueError('isolated_sec_m2_database_required')
        if self.write_enabled and (not self.infra_enabled or len(self.executor_write_key)<32):
            raise ValueError('write_private_credentials_required')
        if not 1<=self.approval_timeout_seconds<=1800: raise ValueError('invalid_approval_timeout')
        if any(len(k) < 32 for k in (self.service_token, self.actor_key, self.envelope_key)):
            raise ValueError("ops_private_credentials_required")
        for url in (self.backend_url, self.supervisor_url, self.executor_url):
            p = urlsplit(url)
            if p.scheme != "http" or not p.hostname or p.username or p.query or p.fragment or p.path:
                raise ValueError("invalid_internal_origin")
        if self.infra_enabled and (min(len(self.executor_token),len(self.executor_grant_key))<32 or urlsplit(self.executor_url).hostname not in ('127.0.0.1','ops-executor')):
            raise ValueError('infra_private_credentials_required')
        if any(getattr(self, f) <= 0 for f in ("task_timeout", "model_timeout", "tool_timeout", "max_rounds", "max_calls", "global_concurrency", "user_active_limit", "queue_capacity", "lease_seconds", "heartbeat_seconds")) or self.task_timeout > 300 or self.max_rounds > 8 or self.max_calls > 16 or self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("invalid_ops_budget")
        if not re.fullmatch(r"[a-zA-Z0-9:_-]{1,80}:", self.redis_prefix): raise ValueError("invalid_redis_prefix")

    def redact(self, text, extra=()):
        for value in (self.llm_key, self.service_token, self.actor_key, self.envelope_key, self.knowledge_token, self.database_url, self.redis_url, self.executor_token,self.executor_grant_key,self.executor_write_key,self.security_service_token,self.security_request_key,self.security_result_key,*extra):
            if value: text = text.replace(value, "[REDACTED]")
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~-]+|\bsk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)
