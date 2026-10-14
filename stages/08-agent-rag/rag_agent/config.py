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
        return cfg

    def redact(self, text, extra=()):
        for value in (self.llm_key, self.service_token, self.search_key, self.database_url,
                      self.redis_url, self.ops_token, self.ma_actor_key, self.ma_envelope_key, *extra):
            if value:
                text = text.replace(value, "[REDACTED]")
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~-]+|\bsk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)
