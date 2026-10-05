from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import urlsplit


def secret_file(name: str, required: bool = True) -> str:
    path = os.getenv(name, "")
    if not path:
        if required:
            raise ValueError(f"{name} is required")
        return ""
    try:
        return Path(path).read_text(encoding="utf-8-sig").strip()
    except OSError:
        raise ValueError(f"{name} is unreadable") from None


def base_url(value: str, cloud: bool = False) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid base URL")
    if cloud and parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "host.docker.internal", "ollama"):
        raise ValueError("remote LLM requires HTTPS")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    backend_url: str
    llm_url: str
    model: str
    api_key: str
    service_token: str
    redis_url: str
    audit_dir: Path
    max_rounds: int = 5
    max_calls: int = 10
    total_timeout: float = 40
    tool_timeout: float = 5
    model_timeout: float = 20
    session_ttl: int = 86400
    monitor_token: str = ""
    model_configured: bool = True
    knowledge_path: Path = Path(__file__).resolve().parents[1] / "knowledge/index.json"
    misses_dir: Path = Path("artifacts/stage7/v2m3/knowledge-misses")
    search_url: str = ""
    search_key: str = ""
    search_provider: str = "generic"

    @classmethod
    def load(cls):
        token = secret_file("AGENT_SERVICE_TOKEN_FILE")
        if len(token) < 32:
            raise ValueError("service token must contain 32+ characters")
        model = os.getenv("AGENT_LLM_MODEL", "").strip()
        llm_url = os.getenv("AGENT_LLM_BASE_URL", "").strip()
        configured = bool(model and llm_url and len(model) <= 128)
        key = ""
        try:
            key = secret_file("AGENT_LLM_API_KEY_FILE", required=False)
        except ValueError:
            configured = False
        if configured:
            try:
                llm_url = base_url(llm_url, cloud=True)
                if urlsplit(llm_url).hostname not in ("localhost", "127.0.0.1", "host.docker.internal", "ollama") and not key:
                    configured = False
            except ValueError:
                configured = False
        search_provider = os.getenv("AGENT_SEARCH_PROVIDER", "generic").strip().lower()
        if search_provider not in ("generic", "bocha"):
            raise ValueError("unsupported search provider")
        search_url = os.getenv("AGENT_SEARCH_URL", "").strip()
        if search_provider == "bocha":
            from .search import BOCHA_URL
            if search_url and search_url != BOCHA_URL:
                raise ValueError("Bocha requires the official fixed endpoint")
            search_url = BOCHA_URL
        search_key = ""
        if search_url:
            from .search import safe_url
            if not safe_url(search_url):
                raise ValueError("search API requires a public HTTPS endpoint")
            try:
                search_key = secret_file("AGENT_SEARCH_KEY_FILE", required=False)
            except ValueError:
                pass  # Optional integration degrades independently of the Agent.
        return cls(
            backend_url=base_url(os.getenv("AGENT_BACKEND_URL", "http://127.0.0.1:8080")),
            llm_url=llm_url if configured else "",
            model=model,
            api_key=key,
            service_token=token,
            redis_url=os.environ["AGENT_REDIS_URL"],
            audit_dir=Path(os.getenv("AGENT_AUDIT_DIR", "../../artifacts/stage7/runtime")),
            monitor_token=secret_file("AGENT_MONITOR_TOKEN_FILE", required=False),
            model_configured=configured,
            knowledge_path=Path(os.getenv("AGENT_KNOWLEDGE_PATH", str(cls.knowledge_path))),
            misses_dir=Path(os.getenv("AGENT_MISSES_DIR", "artifacts/stage7/v2m3/knowledge-misses")),
            search_url=search_url,
            search_key=search_key,
            search_provider=search_provider,
        )
