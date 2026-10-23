import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from agent.config import Config
from agent.interpret_provider import InterpretationProvider, PAUSE_KEY
from agent.interpret_v2 import Analysis, probe_event, validate_analysis, run_event
from agent.llm import ProviderError

CFG = Config(backend_url="http://api", llm_url="https://old.invalid", model="old-chat", api_key="old-secret",
             service_token="service-test-token", redis_url="redis://localhost", audit_dir=Path("/tmp/audit"),
             interpret_api_key="new-event-secret", interpret_model_configured=True)


def answer(text="证据记录为{{F2}}，需结合现场核查。", refs=None):
    return {"findings": [{"template": text, "fact_refs": refs or ["F2"]}],
            "hypotheses": [], "suggestions": [], "limitations": []}


def envelope(content=None, **changes):
    message = {"role": "assistant", "content": json.dumps(answer()) if content is None else content}
    message.update(changes)
    return {"choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 70, "completion_tokens_details": {"reasoning_tokens": 0}}}


class ProviderTest(unittest.IsolatedAsyncioTestCase):
    async def call(self, response, *, headers=None, status=200, cfg=CFG):
        seen = []
        async def handler(request):
            seen.append(request)
            return httpx.Response(status, content=response, headers=headers or {"content-type": "application/json"})
        redis = AsyncMock(); redis.get.return_value = None
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await InterpretationProvider(cfg, client, redis).complete([{"role": "user", "content": "JSON"}])
        return result, seen, redis

    async def test_official_contract_keepalive_and_no_key_crossing(self):
        result, seen, _ = await self.call(b"\n\n " + json.dumps(envelope()).encode())
        body = json.loads(seen[0].content)
        self.assertEqual(str(seen[0].url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(seen[0].headers["authorization"], "Bearer new-event-secret")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_tokens"], 4096)
        self.assertFalse(body["stream"])
        self.assertTrue({"tools", "tool_choice", "reasoning_effort"}.isdisjoint(body))
        self.assertNotIn("old-secret", str(seen[0].headers))
        self.assertEqual(result["diagnostic"]["usage"]["reasoning_tokens"], 0)

    async def test_protocol_subcodes(self):
        cases = [(b"<html>bad</html>", "model_non_json"),
                 (b'data: [DONE]', "model_unexpected_sse"),
                 (b'{"choices":[]}', "model_empty_choices"),
                 (json.dumps(envelope("")).encode(), "model_empty_content"),
                 (json.dumps(envelope(role="user")).encode(), "model_invalid_role"),
                 (json.dumps(envelope(reasoning_content="not empty")).encode(), "thinking_mode_mismatch")]
        for raw, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ProviderError, reason) as caught:
                await self.call(raw)
            self.assertEqual(caught.exception.diagnostic["http_status"], 200)
            self.assertNotIn("content", caught.exception.diagnostic)

    async def test_null_optional_tools_and_truncation(self):
        await self.call(json.dumps(envelope(tool_calls=None)).encode())
        data = envelope(); data["choices"][0]["finish_reason"] = "length"
        with self.assertRaisesRegex(ProviderError, "answer_truncated"):
            await self.call(json.dumps(data).encode())
        data["choices"][0]["finish_reason"] = {"unexpected": True}
        with self.assertRaisesRegex(ProviderError, "model_invalid_finish"):
            await self.call(json.dumps(data).encode())

    async def test_pause_is_independent(self):
        redis = AsyncMock(); redis.get.return_value = "auth_failed"
        client = AsyncMock(); provider = InterpretationProvider(CFG, client, redis)
        with self.assertRaisesRegex(ProviderError, "auth_failed"):
            await provider.complete([])
        redis.get.assert_awaited_once_with(PAUSE_KEY)
        client.stream.assert_not_called()
        await provider.recover()
        redis.delete.assert_awaited_once_with(PAUSE_KEY)

    async def test_missing_key_no_fallback(self):
        with self.assertRaisesRegex(ProviderError, "not_configured"):
            await self.call(b'{}', cfg=replace(CFG, interpret_model_configured=False))

    def test_config_missing_event_key_does_not_disable_chat(self):
        values={"AGENT_REDIS_URL":"redis://unused","AGENT_LLM_MODEL":"old","AGENT_LLM_BASE_URL":"https://provider.invalid/v1"}
        def key(name,required=True):
            return "s"*40 if name in {"AGENT_SERVICE_TOKEN_FILE","AGENT_LLM_API_KEY_FILE"} else ""
        with patch.dict(os.environ,values,clear=True),patch("agent.config.secret_file",side_effect=key):
            cfg=Config.load()
        self.assertTrue(cfg.model_configured)
        self.assertFalse(cfg.interpret_model_configured)
        self.assertEqual(cfg.interpret_api_key,"")

    async def test_rate_limit_retry_after(self):
        with self.assertRaisesRegex(ProviderError, "rate_limited") as caught:
            await self.call(b'{}', status=429, headers={"retry-after": "125"})
        self.assertEqual(caught.exception.retry_after, 125)
        with self.assertRaisesRegex(ProviderError, "unavailable"):
            await self.call(b'upstream bad gateway', status=502)
        async def timed_out(request):
            raise httpx.ReadTimeout("fixture timeout", request=request)
        redis=AsyncMock();redis.get.return_value=None
        async with httpx.AsyncClient(transport=httpx.MockTransport(timed_out)) as client:
            with self.assertRaisesRegex(ProviderError,"model_timeout"):
                await InterpretationProvider(CFG,client,redis).complete([])


class FactsTest(unittest.IsolatedAsyncioTestCase):
    def test_main_factory_registers_v2(self):
        from fastapi.testclient import TestClient
        from agent.main import app,create_app
        self.assertTrue(any(route.path=="/internal/agent/interpret/v2" for route in app.routes))
        vendor=AsyncMock()
        with TestClient(create_app(config=CFG,sessions=AsyncMock(),provider=vendor,tools=AsyncMock(),audit=AsyncMock())) as client:
            self.assertEqual(client.get('/health').json()['interpret_model_configured'],True)
        vendor.complete.assert_not_called()

    def test_five_evidence_probe_has_100_points(self):
        event = probe_event()
        self.assertEqual(len(event.evidence), 5)
        self.assertEqual(len(event.evidence[1].data["points"]), 100)
        validate_analysis(Analysis.model_validate(answer()), event)

    def test_invalid_reference_numbers_cause_and_probability_binding(self):
        event = probe_event()
        cases = [(answer("记录{{F9}}", ["F9"]), "invalid_fact_reference"),
                 (answer("温度99度", ["F1"]), "unsupported_numeric_claim"),
                 (answer("发生于2026-10-21", ["F1"]), "unsupported_numeric_claim"),
                 (answer("预测概率{{F3}}", ["F3"]), "prediction_value_changed"),
                 (answer("根因是设备损坏", ["F2"]), "unsupported_causal_claim")]
        for data, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                validate_analysis(Analysis.model_validate(data), event)
        validate_analysis(Analysis.model_validate(answer("建议现场确认是否存在设备故障。")),event)
        validate_analysis(Analysis.model_validate(answer("不意味着已经发生故障。")),event)
        demo=probe_event();demo.event_origin="simulation_demo"
        validate_analysis(Analysis.model_validate(answer("受控演示，不代表实际故障。")),demo)
        with self.assertRaisesRegex(ValueError,"unsupported_causal_claim"):
            validate_analysis(Analysis.model_validate(answer("建议核查。根因是设备损坏。")),event)

    async def test_run_audits_metadata_not_prose(self):
        provider, audit = AsyncMock(), AsyncMock(); provider.cfg = CFG
        provider.complete.return_value = {"content": json.dumps(answer()), "diagnostic": {"http_status": 200}}
        result = await run_event(probe_event(), provider, audit)
        self.assertEqual(result["status"], "answered")
        self.assertNotIn("template", json.dumps(audit.write.call_args.args))
        self.assertNotIn("analysis", audit.write.call_args.args[0])

    async def test_schema_and_audit_failure(self):
        provider, audit = AsyncMock(), AsyncMock(); provider.cfg = CFG
        provider.complete.return_value = {"content": '{"extra":1}', "diagnostic": {"http_status": 200}}
        result = await run_event(probe_event(), provider, audit)
        self.assertEqual(result["reason"], "answer_schema_invalid")
        self.assertEqual(result["analysis_status"], "rejected")
        audit.write.side_effect = OSError()
        with self.assertRaises(HTTPException):
            await run_event(probe_event(), provider, audit)


if __name__ == "__main__":
    unittest.main()
