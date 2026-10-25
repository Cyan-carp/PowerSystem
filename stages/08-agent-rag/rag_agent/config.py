import os
import re
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path


def secret(name):
    path = os.getenv(name, "")
    return Path(path).read_text(encoding="utf-8-sig").strip() if path else ""


@dataclass
class Config:
    model: str
    llm_url: str
    llm_key: str
    service_token: str
    database_url: str
    redis_url: str
    backend_url: str = "http://127.0.0.1:8080"
    qdrant_url: str = "http://127.0.0.1:16333"
    embedding_path: str = "intfloat/multilingual-e5-small"
    collection: str = "powersystem_rag_active"
    search_key: str = ""
    audit_dir: Path = Path("artifacts/stage8-agent-rag/audit")
    total_timeout: float = 120
    model_timeout: float = 60
    tool_timeout: float = 15
    max_rounds: int = 8
    max_calls: int = 16
    max_tokens: int = 8192
    input_chars: int = 32000
    context_chars: int = 180000
    tool_chars: int = 60000
    evidence_enabled: bool = True
    verify_enabled: bool = True
    daily_gate_enabled: bool = True
    telemetry_max_age_seconds: float = 120
    future_skew_seconds: float = 60
    verify_max_pages: int = 10
    ops_url: str = "http://127.0.0.1:8094"
    ops_token: str = ""
    ma_actor_key: str = ""
    ma_envelope_key: str = ""
    acceptance_drill_only: bool = False
    model_role: str = "assistant"
    protocol_version: str = "v4-draft1"
    v4_assistant_enabled: bool = False
    model_thinking: str | None = None
    v4_runs_enabled: bool = False
    v4_artifacts_enabled: bool = False
    v4_actions_enabled: bool = False
    v4_ops_enabled: bool = False
    v4_schedules_enabled: bool = False
    v4_joint_profile: str = ''
    v4_read_protocol: str = 'ma-run/v1'
    source_collection_prefix: str | None = None
    schedule_bridge_token: str = ''
    artifact_dir: Path = Path('artifacts/stage8-agent-rag/files')

    @property
    def model_destination(self):
        """Registered model origin without credentials, path or query."""
        try:
            parts = urlsplit(self.llm_url)
            port = f":{parts.port}" if parts.port else ""
        except (TypeError, ValueError):
            return ""
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return ""
        return f"{parts.scheme}://{parts.hostname}{port}"

    def provider_profile(self):
        """Auditable provider registration; never contains credentials."""
        return {"role": self.model_role, "model": self.model,
                "destination": self.model_destination, "protocol_version": self.protocol_version,
                "capabilities": {"json": True, "sse": True, "tools": True, "usage": True},
                "capabilities_scope": "adapter_support", "candidate_validated": False,
                "timeouts": {"model": self.model_timeout, "total": self.total_timeout},
                "output_budget": self.max_tokens, "thinking":self.model_thinking or "provider_default"}

    @classmethod
    def load(cls):
        token = secret("AGENT_SERVICE_TOKEN_FILE")
        if len(token) < 32:
            raise ValueError("service token must contain at least 32 characters")
        cfg = cls(model=os.environ["AGENT_LLM_MODEL"],
            llm_url=os.environ["AGENT_LLM_BASE_URL"].rstrip("/"),
            llm_key=secret("AGENT_LLM_API_KEY_FILE"), service_token=token,
            database_url=os.environ["RAG_DATABASE_URL"], redis_url=os.environ["AGENT_REDIS_URL"],
            backend_url=os.getenv("AGENT_BACKEND_URL", "http://127.0.0.1:8080").rstrip("/"),
            qdrant_url=os.getenv("RAG_QDRANT_URL", "http://127.0.0.1:16333"),
            embedding_path=os.getenv("RAG_EMBEDDING_PATH", "intfloat/multilingual-e5-small"),
            collection=os.getenv("RAG_COLLECTION", "powersystem_rag_active"),
            search_key=secret("AGENT_SEARCH_KEY_FILE"),
            audit_dir=Path(os.getenv("RAG_AUDIT_DIR", "artifacts/stage8-agent-rag/audit")))
        cfg.ops_url = os.getenv("OPS_URL", "http://127.0.0.1:8094").rstrip("/")
        cfg.ops_token = secret("OPS_SERVICE_TOKEN_FILE")
        cfg.ma_actor_key = secret("MA_ACTOR_KEY_FILE")
        cfg.ma_envelope_key = secret("MA_ENVELOPE_KEY_FILE")
        cfg.acceptance_drill_only=os.getenv('MA_DRILL_ACCEPTANCE_ONLY','false').lower()=='true'
        role = os.getenv("AGENT_MODEL_ROLE", "assistant").strip()
        if not role or len(role) > 64:
            raise ValueError("invalid model role")
        cfg.model_role = role
        cfg.model_thinking=os.getenv('RAG_MODEL_THINKING') or None
        if cfg.model_thinking not in (None,'disabled'):
            raise ValueError('invalid model thinking mode')
        # The only new V4 switch owned by S02; default stays off.
        enabled = os.getenv("V4_ASSISTANT_ENABLED", "false").lower()
        if enabled not in ("true", "false", "1", "0"):
            raise ValueError("invalid v4 configuration: v4_assistant_enabled")
        cfg.v4_assistant_enabled = enabled in ("true", "1")
        runs_enabled=os.getenv('V4_RUNS_ENABLED','false').lower()
        if runs_enabled not in ('true','false','1','0'):raise ValueError('invalid v4 configuration: v4_runs_enabled')
        cfg.v4_runs_enabled=runs_enabled in ('true','1')
        actions_enabled=os.getenv('V4_ACTIONS_ENABLED','false').lower()
        if actions_enabled not in ('true','false'):raise ValueError('invalid_v4_actions_switch')
        cfg.v4_actions_enabled=actions_enabled=='true'
        ops_enabled=os.getenv('V4_OPS_WRITE_ENABLED','false').lower()
        if ops_enabled not in ('true','false'):raise ValueError('invalid_v4_ops_switch')
        cfg.v4_ops_enabled=ops_enabled=='true'
        schedules_enabled=os.getenv('V4_SCHEDULES_ENABLED','false').lower()
        if schedules_enabled not in ('true','false'):raise ValueError('invalid_v4_schedules_switch')
        cfg.v4_schedules_enabled=schedules_enabled=='true'
        cfg.v4_joint_profile=os.getenv('V4_JOINT_PROFILE','')
        cfg.schedule_bridge_token=secret('SCHEDULE_BRIDGE_TOKEN_FILE')
        cfg.v4_read_protocol=os.getenv('V4_READ_DELEGATION_PROTOCOL','ma-run/v1')
        cfg.source_collection_prefix=os.getenv('RAG_SOURCE_COLLECTION_PREFIX') or None
        if cfg.source_collection_prefix is not None and (cfg.source_collection_prefix!='powersystem_rag_' or not cfg.collection.startswith('powersystem_rag_')):
            raise ValueError('invalid_registered_source_collection_prefix')
        if cfg.v4_runs_enabled and cfg.ops_url=='http://ops-agent:8094' and 'V4_READ_DELEGATION_PROTOCOL' not in os.environ:
            raise ValueError('explicit_production_read_protocol_required')
        from .legacy_run_read import validate as validate_read_protocol
        validate_read_protocol(cfg)
        if cfg.v4_joint_profile:
            from .joint_profile import validate
            validate(cfg,'rag')
            if len(cfg.schedule_bridge_token)<32 or cfg.schedule_bridge_token==cfg.service_token:raise ValueError('independent_schedule_bridge_identity_required')
        if not cfg.v4_joint_profile and cfg.v4_schedules_enabled and (cfg.v4_actions_enabled or cfg.v4_ops_enabled or urlsplit(cfg.database_url).hostname!='127.0.0.1' or urlsplit(cfg.database_url).port!=25438 or not urlsplit(cfg.database_url).path.startswith('/v4_s08_') or not urlsplit(cfg.database_url).path.endswith('_test') or cfg.ops_url!='http://127.0.0.1:28983' or cfg.backend_url!='http://127.0.0.1:28982'):
            raise ValueError('isolated_s08_policy_required')
        if not cfg.v4_joint_profile and cfg.v4_ops_enabled and (cfg.v4_actions_enabled or urlsplit(cfg.database_url).hostname!='127.0.0.1' or urlsplit(cfg.database_url).port!=25437 or not urlsplit(cfg.database_url).path.startswith('/v4_s07_') or not urlsplit(cfg.database_url).path.endswith('_test') or cfg.ops_url!='http://127.0.0.1:28973'):
            raise ValueError('isolated_s07_policy_required')
        if not cfg.v4_joint_profile and cfg.v4_actions_enabled and (urlsplit(cfg.database_url).hostname!='127.0.0.1' or urlsplit(cfg.database_url).port!=25436 or not urlsplit(cfg.database_url).path.startswith('/v4_s06_') or not urlsplit(cfg.database_url).path.endswith('_test') or cfg.ops_url!='http://127.0.0.1:28963'):
            raise ValueError('isolated_s06_actions_required')
        legacy_drill = cfg.database_url.endswith('/ma_m3_rag_test') and cfg.backend_url == 'http://127.0.0.1:18103'
        sec_drill = cfg.database_url.endswith('/sec_m2_rag_acceptance_test') and cfg.backend_url == 'http://127.0.0.1:18104'
        sec_inspection=cfg.database_url.endswith('/sec_m3_rag_acceptance_test') and cfg.backend_url=='http://127.0.0.1:18105' and cfg.ops_url=='http://127.0.0.1:8111'
        if cfg.acceptance_drill_only and not (legacy_drill or sec_drill or sec_inspection or (cfg.database_url.endswith("/sec_m4_rag_acceptance_test") and cfg.backend_url=="http://127.0.0.1:18106" and cfg.ops_url=="http://127.0.0.1:8115")):
            raise ValueError('dedicated_drill_acceptance_required')
        origin = urlsplit(cfg.ops_url)
        if origin.scheme != "http" or not origin.hostname or origin.username or origin.path or origin.query or origin.fragment:
            raise ValueError("invalid_ops_origin")
        for field in ("total_timeout", "model_timeout", "tool_timeout", "max_rounds", "max_calls",
                      "max_tokens", "input_chars", "context_chars", "tool_chars"):
            value = os.getenv("RAG_" + field.upper())
            if value:
                setattr(cfg, field, type(getattr(cfg, field))(value))
            if getattr(cfg, field) <= 0:
                raise ValueError("invalid resource configuration: " + field)
        for field in ("evidence_enabled", "verify_enabled", "daily_gate_enabled"):
            cfg_value = os.getenv("BND_" + field.upper(), "true").lower()
            if cfg_value not in ("true", "false", "1", "0"):
                raise ValueError("invalid boundary configuration: " + field)
            setattr(cfg, field, cfg_value in ("true", "1"))
        for field in ("telemetry_max_age_seconds", "future_skew_seconds", "verify_max_pages"):
            value = os.getenv("BND_" + field.upper())
            if value:
                setattr(cfg, field, type(getattr(cfg, field))(value))
            if getattr(cfg, field) <= 0:
                raise ValueError("invalid boundary configuration: " + field)
        artifacts_enabled = os.getenv('V4_ARTIFACTS_ENABLED', 'false').lower()
        if artifacts_enabled not in ('true','false','1','0'): raise ValueError('invalid v4 configuration: v4_artifacts_enabled')
        cfg.v4_artifacts_enabled = artifacts_enabled in ('true','1')
        cfg.artifact_dir = Path(os.getenv('RAG_ARTIFACT_DIR', 'artifacts/stage8-agent-rag/files'))
        return cfg

    def redact(self, text, extra=()):
        for value in (self.llm_key, self.service_token, self.search_key, self.database_url,
                      self.redis_url, self.ops_token, self.ma_actor_key, self.ma_envelope_key, *extra):
            if value:
                text = text.replace(value, "[REDACTED]")
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~-]+|\bsk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)
