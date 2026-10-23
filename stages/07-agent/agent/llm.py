import json
import httpx

# Stage 07 keeps the small default budget for chat/tool calls; only callers that
# need a larger budget (event interpretation) may override it per call.
DEFAULT_MAX_TOKENS = 1500
MAX_OUTPUT_TOKENS = 8192
FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter"}
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def safe_usage(value):
    """Keep only bounded integer token counts; never vendor text or details."""
    if not isinstance(value, dict):
        return {}
    usage = {}
    for name in USAGE_FIELDS:
        count = value.get(name)
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 10**9:
            usage[name] = count
    details = value.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else value.get("reasoning_tokens")
    if isinstance(reasoning, int) and not isinstance(reasoning, bool) and 0 <= reasoning <= 10**9:
        usage["reasoning_tokens"] = reasoning
    return usage


class ProviderError(Exception):
    """Deliberately contains only a safe reason, never vendor body or headers."""
    def __init__(self, reason, retry_after=0, http_status=None, validation_code=""):
        super().__init__(reason)
        self.retry_after = retry_after
        self.http_status = http_status
        self.validation_code = validation_code


def classify_error(status, raw, retry_after=""):
    # Only explicit allowlisted supplier codes establish quota exhaustion.
    code = ""
    try:
        error = json.loads(raw).get("error", {})
        code = str(error.get("code") or error.get("type") or "").lower() if isinstance(error, dict) else ""
    except (ValueError, AttributeError):
        pass
    if code in {"insufficient_quota", "quota_exhausted", "balance_not_enough", "insufficient_balance"}:
        reason = "quota_exhausted"
    elif status in (401, 403) or code in {"invalid_api_key", "api_key_expired", "expired_api_key"}:
        reason = "auth_failed"
    elif status == 429:
        reason = "rate_limited"
    else:
        reason = "unavailable"
    try:
        delay = min(60, max(0, int(retry_after)))
    except (ValueError, TypeError):
        delay = 0
    return ProviderError(reason, delay, status)


class Provider:
    def __init__(self, config, client: httpx.AsyncClient):
        self.config, self.client = config, client

    async def complete(self, messages, tools=None, max_tokens=None):
        if not self.config.model_configured:
            raise ProviderError("not_configured")
        budget = DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        if not isinstance(budget, int) or isinstance(budget, bool) or not 0 < budget <= MAX_OUTPUT_TOKENS:
            raise ProviderError("model_output_budget_invalid")
        body = {"model": self.config.model, "messages": messages, "stream": False,
                "max_tokens": budget}
        if tools:
            body["tools"] = tools
            # Forcing a named function was rejected by the compatible gateway;
            # auto keeps the caller's original schema unchanged.
            body["tool_choice"] = "auto"
        # The authentication header is separate; known credentials are never
        # allowed in model content, even if pasted by a caller.
        raw = json.dumps(body, ensure_ascii=False)
        for secret in (self.config.api_key, self.config.interpret_api_key, self.config.service_token, self.config.monitor_token, self.config.search_key):
            if secret:
                raw = raw.replace(secret, "[REDACTED]")
        if len(raw.encode()) > 163840:
            raise ProviderError("model_context_budget_exceeded")
        body = json.loads(raw)
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        try:
            async with self.client.stream("POST", self.config.llm_url + "/chat/completions",
                                          headers=headers, json=body, timeout=self.config.model_timeout) as response:
                if response.status_code != 200:
                    error_raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        error_raw.extend(chunk)
                        if len(error_raw) > 16384:
                            break
                    raise classify_error(response.status_code, error_raw[:16384], response.headers.get("retry-after", ""))
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 262144:
                        raise ProviderError("model_response_too_large")
                data = json.loads(raw)
            choice = data["choices"][0]
            if not isinstance(choice, dict):
                raise ValueError()
            message = choice["message"]
            # Some compatible gateways emit an empty role for an otherwise
            # valid completion. Normalize only this omission; explicit user,
            # system or tool roles remain invalid and answer checks still run.
            if not isinstance(message, dict) or message.get("role", "assistant") not in ("assistant", ""):
                raise ValueError()
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or len(calls) > 10:
                raise ValueError()
            finish_reason = choice.get("finish_reason")
            if not isinstance(finish_reason, str) or finish_reason not in FINISH_REASONS:
                finish_reason = ""
            # Drop unrecognized provider fields (e.g. hidden reasoning); only the
            # allowlisted finish reason and numeric usage survive.
            return {"role": "assistant", "content": message.get("content"),
                    **({"tool_calls": calls} if calls else {}),
                    "finish_reason": finish_reason, "usage": safe_usage(data.get("usage"))}
        except httpx.TimeoutException:
            raise ProviderError("model_timeout") from None
        except httpx.HTTPError:
            raise ProviderError("model_unreachable") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("model_invalid_response") from None

    async def probe(self):
        first = await self.complete([{"role": "user", "content": "Reply OK."}])
        if not isinstance(first.get("content"), str) or not first["content"]:
            raise ProviderError("model_chat_unsupported")
        tools = [{"type": "function", "function": {"name": "probe", "description": "Return the supplied integer.",
                  "parameters": {"type": "object", "properties": {"value": {"type": "integer"}},
                                 "required": ["value"], "additionalProperties": False}}}]
        second = await self.complete([{"role": "user", "content": "Call probe with value 7. Do not answer in text."}], tools)
        try:
            call = second["tool_calls"][0]
            if call["type"] != "function" or call["function"]["name"] != "probe" or not call["id"]:
                raise ValueError()
            if json.loads(call["function"]["arguments"]) != {"value": 7}:
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("model_tools_unsupported") from None
        return {"chat": True, "tools": True, "model": self.config.model}
