import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def secret(name):
    value = os.getenv(name, '')
    return Path(value).read_text(encoding='utf-8-sig').strip() if value else ''


@dataclass
class Config:
    service_token: str = field(repr=False)
    request_key: str = field(repr=False)
    result_key: str = field(repr=False)
    llm_key: str = field(default='', repr=False)
    enabled: bool = False
    llm_url: str = 'https://hgapi.dieqiyun.top/v1'
    model: str = ''
    model_timeout: float = 30
    review_ttl: int = 300
    global_concurrency: int = 2
    user_concurrency: int = 1
    max_tokens: int = 2048
    acceptance_timeout_exception: bool = False
    runtime_profile: str = "legacy"

    @classmethod
    def load(cls):
        raw = os.getenv('SEC_ENABLED', 'false').lower()
        if raw not in ('true', 'false'): raise ValueError('invalid_security_switch')
        cfg = cls(secret('SEC_SERVICE_TOKEN_FILE'), secret('SEC_REQUEST_KEY_FILE'), secret('SEC_RESULT_KEY_FILE'),
                  secret('SEC_LLM_API_KEY_FILE'), raw == 'true', os.getenv('SEC_LLM_URL', 'https://hgapi.dieqiyun.top/v1'),
                  os.getenv('SEC_MODEL', ''))
        cfg.model_timeout = float(os.getenv('SEC_MODEL_TIMEOUT_SECONDS', '30'))
        cfg.review_ttl = int(os.getenv('SEC_REVIEW_TTL_SECONDS', '300'))
        exception = os.getenv('SEC_ACCEPTANCE_TIMEOUT_EXCEPTION_ENABLED', 'false').lower()
        if exception not in ('true', 'false'): raise ValueError('invalid_security_timeout_exception')
        cfg.acceptance_timeout_exception = exception == 'true'
        cfg.runtime_profile = os.getenv("SEC_RUNTIME_PROFILE", "legacy")
        cfg.validate()
        return cfg

    def validate(self):
        if self.runtime_profile not in ("legacy", "m4-local", "m4-server-readonly"): raise ValueError("unknown_security_profile")
        if self.runtime_profile != "legacy" and (self.model != "deepseek-v4-flash-0731" or self.model_timeout != 30 or self.acceptance_timeout_exception): raise ValueError("m4_model_configuration_required")
        if min(map(len, (self.service_token, self.request_key, self.result_key))) < 32:
            raise ValueError('security_private_credentials_required')
        if len({self.service_token, self.request_key, self.result_key}) != 3:
            raise ValueError('independent_security_credentials_required')
        if self.enabled and (not self.model or not self.llm_key): raise ValueError('explicit_security_model_required')
        url = urlsplit(self.llm_url)
        if self.llm_url != 'https://hgapi.dieqiyun.top/v1' or url.username or url.query or url.fragment:
            raise ValueError('unlicensed_security_destination')
        if type(self.acceptance_timeout_exception) is not bool or (self.acceptance_timeout_exception and self.model != 'gpt-5.6-sol'):
            raise ValueError('invalid_security_timeout_exception')
        limit = 120 if self.acceptance_timeout_exception else 30
        if not 0 < self.model_timeout <= limit or not 1 <= self.review_ttl <= 300:
            raise ValueError('invalid_security_budget')
        if (self.global_concurrency, self.user_concurrency) != (2, 1): raise ValueError('invalid_security_concurrency')

    def secrets(self):
        return (self.service_token, self.request_key, self.result_key, self.llm_key)
