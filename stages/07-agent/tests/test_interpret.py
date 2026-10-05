import json
import os
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from agent.config import Config
from agent.llm import Provider, ProviderError, classify_error
from agent.interpret import GuardedProvider, PAUSE_KEY
from agent.main import create_app
from agent.orchestrator import validate_answer
from agent.schemas import ModelAnswer
from tests.test_agent import CFG, answer, evidence


class ModelFailureTest(unittest.IsolatedAsyncioTestCase):
    def test_error_classification(self):
        self.assertEqual(str(classify_error(429, b'{"error":{"code":"insufficient_quota"}}')), "quota_exhausted")
        self.assertEqual(str(classify_error(429, b'unknown')), "rate_limited")
        self.assertEqual(str(classify_error(401, b'secret')), "auth_failed")
        self.assertEqual(str(classify_error(400, b'{"error":{"code":"api_key_expired"}}')), "auth_failed")
        self.assertEqual(str(classify_error(500, b'secret')), "unavailable")
        self.assertEqual(classify_error(429, b'{}', "999").retry_after, 60)

    def test_missing_model_can_load(self):
        with patch.dict(os.environ, {"AGENT_REDIS_URL": "redis://localhost", "AGENT_LLM_MODEL": "", "AGENT_LLM_BASE_URL": ""}, clear=True), patch("agent.config.secret_file", return_value="s"*40):
            self.assertFalse(Config.load().model_configured)

    def test_empty_remote_key_and_optional_local_key(self):
        def read_secret(name,required=True): return 's'*40 if name=='AGENT_SERVICE_TOKEN_FILE' else ''
        for url,expected in (('https://provider.invalid/v1',False),('http://ollama:11434/v1',True)):
            with patch.dict(os.environ,{'AGENT_REDIS_URL':'redis://localhost','AGENT_LLM_MODEL':'test','AGENT_LLM_BASE_URL':url,'AGENT_LLM_API_KEY_FILE':'empty-test-key'},clear=True),patch('agent.config.secret_file',side_effect=read_secret):
                self.assertEqual(Config.load().model_configured,expected)

    def test_nested_timestamp_precision_without_new_numbers(self):
        item=evidence()
        item.data['created_at']='2026-10-01T12:53:51.123456+08:00'
        validate_answer(ModelAnswer.model_validate(answer('创建于2026-10-01 12:53:51')), [item])
        with self.assertRaisesRegex(ValueError,'unsupported_numeric_claim'):
            validate_answer(ModelAnswer.model_validate(answer('每小时增加99')), [item])

    def test_mixed_metric_thresholds_remain_bound(self):
        item=evidence(); item.data['history']=[{'metric':'temperature','threshold':60,'value':70}]
        validate_answer(ModelAnswer.model_validate(answer('预测概率0.72，阈值0.65；温度值70超过阈值60')), [item])
        for text in ('预测概率0.72，阈值60；温度值70超过阈值60','温度阈值0.65，预测阈值0.65'):
            with self.assertRaisesRegex(ValueError,'prediction_value_changed'):
                validate_answer(ModelAnswer.model_validate(answer(text)), [item])

    async def test_unconfigured_makes_no_request(self):
        client = AsyncMock()
        with self.assertRaisesRegex(ProviderError, "not_configured"):
            await Provider(replace(CFG, model_configured=False), client).complete([])
        client.stream.assert_not_called()

    async def test_pause_shared_and_probe_restores(self):
        store = {}
        redis = AsyncMock()
        redis.get.side_effect = lambda key: store.get(key)
        async def set_key(key, value): store[key] = value
        async def delete_key(key): store.pop(key, None)
        redis.set.side_effect = set_key; redis.delete.side_effect = delete_key
        vendor = AsyncMock(); vendor.complete.side_effect = ProviderError("quota_exhausted")
        guarded = GuardedProvider(vendor, redis, CFG)
        for _ in range(2):
            with self.assertRaisesRegex(ProviderError, "quota_exhausted"): await guarded.complete([])
        self.assertEqual(vendor.complete.await_count, 1)
        self.assertEqual(store[PAUSE_KEY], "quota_exhausted")
        vendor.probe.return_value = {"chat": True, "tools": True}
        await guarded.probe()
        self.assertTrue((await guarded.state())["available"])

    async def test_rate_limit_does_not_permanently_pause(self):
        redis = AsyncMock(); redis.get.return_value = None
        vendor = AsyncMock(); vendor.complete.side_effect = ProviderError("rate_limited")
        with self.assertRaises(ProviderError): await GuardedProvider(vendor, redis, CFG).complete([])
        redis.set.assert_not_called()


class InterpretationHTTPTest(unittest.TestCase):
    def app(self, vendor=None, audit=None):
        cfg = replace(CFG, monitor_token="m"*40)
        provider = vendor or AsyncMock()
        provider.complete.return_value = {"content": json.dumps(answer())}
        return create_app(cfg, AsyncMock(), provider, AsyncMock(), audit or AsyncMock())

    def headers(self): return {"X-PowerSystem-Token": CFG.service_token}
    def body(self): return {"event_key": "alarm:1", "category": "prediction_risk", "evidence": [evidence().model_dump()]}

    def test_event_only_and_success(self):
        vendor = AsyncMock()
        with TestClient(self.app(vendor)) as client:
            self.assertEqual(client.post("/internal/agent/interpret", json=self.body()).status_code, 401)
            result = client.post("/internal/agent/interpret", headers=self.headers(), json=self.body()).json()
            self.assertEqual(result["status"], "answered")
            self.assertIn("需人工确认", result["suggestions"][0]["text"])
        self.assertEqual(vendor.complete.call_args.args[1][0]["function"]["name"], "submit_interpretation")
        # The sole function is a response schema; no GET, JWT or business tools.

    def test_output_function_never_executes_business_tools(self):
        for name, expected in (("submit_interpretation", "answered"), ("ack_alarm", "degraded")):
            vendor=AsyncMock(); app=self.app(vendor)
            vendor.complete.return_value={"content":None,"tool_calls":[{"type":"function","function":{"name":name,"arguments":json.dumps(answer())}}]}
            with TestClient(app) as client:
                self.assertEqual(client.post("/internal/agent/interpret",headers=self.headers(),json=self.body()).json()['status'],expected)

    def test_request_budget_before_model(self):
        vendor=AsyncMock(); body=self.body();body['evidence'][0]['data']['oversize']='a'*262144
        with TestClient(self.app(vendor)) as client:
            self.assertEqual(client.post('/internal/agent/interpret',headers=self.headers(),json=body).status_code,413)
        vendor.complete.assert_not_called()

    def test_invalid_reference_and_probability_rejected(self):
        for candidate in (answer(ref="E9"), answer("风险概率为65%")):
            vendor = AsyncMock()
            app = self.app(vendor); vendor.complete.return_value = {"content": json.dumps(candidate)}
            with TestClient(app) as client:
                result = client.post("/internal/agent/interpret", headers=self.headers(), json=self.body()).json()
                self.assertEqual(result["status"], "degraded")
                self.assertEqual(result["reason"], "answer_validation_failed")
                self.assertIsNone(result["conclusion"])

    def test_monitor_cannot_invent_root_cause(self):
        body=self.body();body['category']='platform_monitor'
        vendor=AsyncMock();app=self.app(vendor)
        vendor.complete.return_value={'content':json.dumps(answer('API故障的原因是磁盘已满导致进程退出'))}
        with TestClient(app) as client:
            result=client.post('/internal/agent/interpret',headers=self.headers(),json=body).json()
            self.assertEqual(result['reason'],'answer_validation_failed')
            self.assertIsNone(result['conclusion'])

    def test_missing_primary_evidence_does_not_call_model(self):
        vendor = AsyncMock(); body = self.body(); body["evidence"][0]["status"] = "no_data"
        with TestClient(self.app(vendor)) as client:
            self.assertEqual(client.post("/internal/agent/interpret", headers=self.headers(), json=body).json()["status"], "unable_to_determine")
        vendor.complete.assert_not_called()

    def test_audit_failure_rejects_success(self):
        audit = AsyncMock(); audit.write.side_effect = OSError()
        with TestClient(self.app(audit=audit)) as client:
            self.assertEqual(client.post("/internal/agent/interpret", headers=self.headers(), json=self.body()).status_code, 503)

    def test_monitor_store_and_retry(self):
        payload = {"version":"4", "groupKey":"test", "truncatedAlerts":0, "status":"firing", "receiver":"agent",
                   "groupLabels":{}, "commonLabels":{}, "commonAnnotations":{}, "externalURL":"",
                   "alerts":[{"status":"firing", "labels":{"alertname":"PowerSystemAPIDown"}, "annotations":{},
                              "startsAt":"2026-10-01T00:00:00Z", "endsAt":"0001-01-01T00:00:00Z", "fingerprint":"test"}]}
        app = self.app()
        with TestClient(app) as client:
            redis = AsyncMock(); app.state.redis = redis
            headers = {"Authorization":"Bearer " + "m"*40}
            self.assertEqual(client.post("/internal/agent/monitor-events", json=payload).status_code, 401)
            self.assertEqual(client.post("/internal/agent/monitor-events", headers=headers, json=payload).status_code, 200)
            redis.xadd.assert_awaited_once()
            redis.xadd.side_effect = ConnectionError()
            self.assertEqual(client.post("/internal/agent/monitor-events", headers=headers, json=payload).status_code, 503)
            payload["alerts"][0]["labels"]["alertname"] = "OtherPlatform"
            self.assertEqual(client.post("/internal/agent/monitor-events", headers=headers, json=payload).status_code, 422)


if __name__ == "__main__": unittest.main()
