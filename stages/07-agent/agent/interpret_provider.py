"""Dedicated, non-thinking official DeepSeek transport; never retries."""
import asyncio
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from redis.exceptions import RedisError

from .llm import ProviderError, safe_usage, classify_error

PAUSE_KEY = "stage7:interpret:model:pause"
PERMANENT = {"auth_failed", "quota_exhausted"}


def retry_delay(value):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, min(86400, int(delay)))


class InterpretationProvider:
    def __init__(self, cfg, client, redis):
        self.cfg, self.client, self.redis = cfg, client, redis

    async def state(self):
        if not self.cfg.interpret_model_configured:
            return {"available": False, "reason": "not_configured", "message": "告警解读专属官方模型尚未配置。"}
        try:
            reason = await self.redis.get(PAUSE_KEY)
        except RedisError:
            raise ProviderError("model_state_unavailable") from None
        return {"available": not bool(reason), "reason": reason or "ready",
                "message": "告警解读专属模型已配置；不代表实时可用性。" if not reason else "告警解读专属通道已暂停，请管理员检测。"}

    async def complete(self, messages, *, ignore_pause=False):
        if not self.cfg.interpret_model_configured:
            raise ProviderError("not_configured")
        if not ignore_pause:
            state = await self.state()
            if not state["available"]:
                raise ProviderError(state["reason"])
        body = {"model": self.cfg.interpret_model, "messages": messages,
                "thinking": {"type": "disabled"}, "stream": False,
                "response_format": {"type": "json_object"}, "max_tokens": self.cfg.interpret_max_tokens}
        serialized = json.dumps(body, ensure_ascii=False)
        for secret in (self.cfg.api_key, self.cfg.interpret_api_key, self.cfg.service_token,
                       self.cfg.monitor_token, self.cfg.search_key):
            if secret:
                serialized = serialized.replace(secret, "[REDACTED]")
        if len(serialized.encode()) > 163840:
            raise ProviderError("model_context_budget_exceeded")
        diagnostic = {"provider": "deepseek_official", "thinking": "disabled"}
        started = time.monotonic()
        try:
            # An absolute deadline is required: keep-alive blank lines must not
            # reset the total budget indefinitely.
            async with asyncio.timeout(self.cfg.interpret_model_timeout):
                async with self.client.stream("POST", self.cfg.interpret_llm_url + "/chat/completions",
                        json=json.loads(serialized), headers={"Authorization": "Bearer " + self.cfg.interpret_api_key,
                        "Content-Type": "application/json"}, timeout=self.cfg.interpret_model_timeout) as response:
                    diagnostic["http_status"] = response.status_code
                    ctype = response.headers.get("content-type", "").split(";")[0].lower()
                    diagnostic["content_type"] = ctype if ctype in {"application/json", "text/event-stream", "text/html", "text/plain"} else "other"
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 262144:
                            raise ProviderError("model_response_too_large")
                    diagnostic["response_bytes"] = len(raw)
                    if response.status_code != 200:
                        error = classify_error(response.status_code, raw[:16384])
                        error.retry_after = retry_delay(response.headers.get("retry-after", ""))
                        raise error
                    if ctype == "text/event-stream" or raw.lstrip().startswith((b"data:", b": keep-alive")):
                        raise ProviderError("model_unexpected_sse")
                    try:
                        data = json.loads(raw)  # JSON permits official blank-line keep-alives.
                    except (ValueError, UnicodeError):
                        raise ProviderError("model_non_json") from None
                    if not isinstance(data, dict):
                        raise ProviderError("model_invalid_envelope")
                    if "error" in data:
                        raise classify_error(200, raw[:16384])
                    choices = data.get("choices")
                    if not isinstance(choices, list) or not choices:
                        raise ProviderError("model_empty_choices")
                    choice = choices[0]
                    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                        raise ProviderError("model_invalid_envelope")
                    message = choice["message"]
                    finish = choice.get("finish_reason")
                    diagnostic["finish_reason"] = finish if isinstance(finish, str) and finish in {"stop", "length", "content_filter", "tool_calls"} else "unknown"
                    diagnostic["usage"] = safe_usage(data.get("usage"))
                    if message.get("role") != "assistant":
                        raise ProviderError("model_invalid_role")
                    if message.get("tool_calls"):
                        raise ProviderError("model_unexpected_tools")
                    if message.get("reasoning_content") or diagnostic["usage"].get("reasoning_tokens", 0) > 0:
                        raise ProviderError("thinking_mode_mismatch")
                    if finish == "length":
                        raise ProviderError("answer_truncated")
                    if finish != "stop":
                        raise ProviderError("model_invalid_finish")
                    content = message.get("content")
                    if not isinstance(content, str) or not content.strip():
                        raise ProviderError("model_empty_content")
                    diagnostic["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                    return {"content": content, "diagnostic": diagnostic}
        except (httpx.TimeoutException, TimeoutError):
            error = ProviderError("model_timeout")
        except httpx.HTTPError:
            error = ProviderError("model_unreachable")
        except ProviderError as exc:
            error = exc
        if str(error) in PERMANENT:
            try:
                await self.redis.set(PAUSE_KEY, str(error))
            except RedisError:
                error = ProviderError("model_state_unavailable")
        diagnostic["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        error.diagnostic = diagnostic
        raise error from None

    async def recover(self):
        try:
            await self.redis.delete(PAUSE_KEY)
        except RedisError:
            raise ProviderError("model_state_unavailable") from None
