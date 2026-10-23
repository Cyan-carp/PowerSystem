"""The model probe must cover the same output schema and guards as events."""
import json
import unittest
from unittest.mock import AsyncMock

from agent.interpret import GuardedProvider, OUTPUT_SCHEMA, probe_interpretation, probe_evidence, validate_event_answer
from agent.schemas import ModelAnswer
from agent.llm import ProviderError
from tests.test_agent import CFG, answer


class InterpretationProbeTest(unittest.IsolatedAsyncioTestCase):
    def test_explicit_alarm_threshold_with_prediction_reference(self):
        evidence = probe_evidence()
        candidate = answer("告警阈值为40，预测概率0.71，预测阈值0.65")
        candidate["conclusion"]["evidence_ids"] = ["E1", "E4"]
        validate_event_answer(ModelAnswer.model_validate(candidate), evidence, "device_alarm")
        for text in ("预测阈值40", "告警阈值0.65"):
            candidate["conclusion"]["text"] = text
            with self.assertRaisesRegex(ValueError, "prediction_value_changed"):
                validate_event_answer(ModelAnswer.model_validate(candidate), evidence, "device_alarm")

    async def test_full_schema_and_production_shaped_synthetic_evidence(self):
        provider = AsyncMock()
        provider.complete.return_value = {"content": json.dumps(answer("合成协议检查"))}
        await probe_interpretation(provider, 4096)
        messages, tools = provider.complete.call_args.args
        self.assertEqual(tools, OUTPUT_SCHEMA)
        self.assertEqual(provider.complete.call_args.kwargs["max_tokens"], 4096)
        event = json.loads(messages[-1]["content"])
        self.assertEqual(event["event_key"], "protocol:model-check")
        self.assertEqual(len(event["evidence"]), 4)
        self.assertTrue(all(item["source"].startswith("synthetic://model-check") for item in event["evidence"]))
        telemetry = event["evidence"][1]
        self.assertEqual(telemetry["tool"], "get_telemetry")
        self.assertEqual(len(telemetry["data"]["points"]), 100)
        points = telemetry["data"]["points"]
        alarm, prediction = event["evidence"][0]["data"], event["evidence"][3]["data"]
        self.assertEqual(alarm["value"], points[-1][1])
        self.assertTrue(all(point[1] > alarm["threshold"] for point in points))
        self.assertGreater(prediction["probability"], prediction["threshold"])
        self.assertAlmostEqual(telemetry["data"]["statistics"]["mean"], sum(p[1] for p in points) / len(points))

    async def test_invalid_reference_and_numeric_claim_fail(self):
        for candidate in (answer("合成协议检查", ref="E9"), answer("温度为99度")):
            provider = AsyncMock()
            provider.complete.return_value = {"content": json.dumps(candidate)}
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ProviderError, "answer_validation_failed") as caught:
                await probe_interpretation(provider, 4096)
            self.assertIn(caught.exception.validation_code, {"invalid_evidence_reference", "unsupported_numeric_claim"})

    async def test_wrong_output_tool_fails(self):
        provider = AsyncMock()
        provider.complete.return_value = {"content": None, "tool_calls": [{"type": "function",
            "function": {"name": "ack_alarm", "arguments": json.dumps(answer("合成协议检查"))}}]}
        with self.assertRaisesRegex(ProviderError, "answer_validation_failed"):
            await probe_interpretation(provider, 4096)

    async def test_admin_probe_is_a_single_representative_model_call(self):
        provider, redis = AsyncMock(), AsyncMock()
        provider.complete.return_value = {"content": json.dumps(answer("合成协议检查"))}
        guarded = GuardedProvider(provider, redis, CFG)
        result = await guarded.probe()
        self.assertEqual(result, {"interpretation": True, "model": CFG.model})
        provider.complete.assert_awaited_once()
        self.assertFalse(provider.probe.called)
        redis.delete.assert_awaited_once()

    async def test_failed_real_shape_probe_does_not_clear_pause(self):
        provider, redis = AsyncMock(), AsyncMock()
        provider.complete.side_effect = ProviderError("unavailable")
        guarded = GuardedProvider(provider, redis, CFG)
        with self.assertRaisesRegex(ProviderError, "unavailable"):
            await guarded.probe()
        redis.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
