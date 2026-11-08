import unittest
import io
import json
from unittest.mock import patch

from app import Handler, business_message, message, send, sign


class AdapterTests(unittest.TestCase):
    def test_signed_payload_and_rejection(self):
        captured = {}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, *_):
                return b'{"code":0}'

        def fake_open(request, timeout):
            captured.update(__import__("json").loads(request.data))
            self.assertEqual(timeout, 8)
            return Reply()

        with patch("app.urlopen", fake_open), patch("app.time", return_value=1700000000):
            send("https://open.feishu.cn/test", "private", "PowerSystem test")
        self.assertEqual(captured["sign"], sign(1700000000, "private"))
        self.assertEqual(captured["content"]["text"], "PowerSystem test")

    def test_alert_statuses(self):
        payload = {"alerts": [{"status": "firing", "labels": {"alertname": "PowerSystemAPIDown"}},
                              {"status": "resolved", "labels": {"alertname": "PowerSystemAPIDown"}}]}
        result = message(payload)
        self.assertIn("触发", result)
        self.assertIn("恢复", result)

    def test_business_alarm_lifecycle_and_invalid_payload(self):
        event = {"event_id": "alarm:42:triggered", "category": "device_alarm", "state": "triggered",
                 "alarm_id": 42, "device_code": "INV-1001", "station_code": "ST-01",
                 "metric": "temperature", "level": "major", "value": 81.5,
                 "threshold": 80.0, "occurred_at": "2026-09-29T12:00:00Z"}
        text = json.dumps(business_message(event), ensure_ascii=False)
        self.assertIn("设备越限", text)
        self.assertIn("触发告警", text)
        self.assertIn("温度", text)
        self.assertIn("重要", text)
        self.assertIn("触发时记录值 **81.500** ／ 触发阈值 80.000", text)
        event.update(event_id="alarm:42:acknowledged", state="acknowledged")
        self.assertIn("已确认", json.dumps(business_message(event), ensure_ascii=False))
        event.update(event_id="alarm:42:recovered", state="recovered", category="prediction_risk", metric="ai_failure_risk")
        recovered = json.dumps(business_message(event), ensure_ascii=False)
        self.assertIn("AI 故障风险", recovered)
        self.assertIn("已恢复", recovered)
        self.assertIn("2026-09-29 20:00:00", recovered)
        self.assertIn("触发时记录值 81.500 ／ 触发阈值 80.000", recovered)
        event.update(event_id="alarm:42:closed", state="closed")
        self.assertIn("触发时记录值 81.500", json.dumps(business_message(event), ensure_ascii=False))
        event["event_id"] = "alarm:41:recovered"
        with self.assertRaises(ValueError):
            business_message(event)

    def test_inbox_expiry_notice_is_bounded_and_not_a_recovery(self):
        event = {"event_id": "inbox-expiry:202611071400", "category": "inbox_expiry",
                 "state": "quarantined", "rows": 3, "first_inbox_id": 100,
                 "last_inbox_id": 105, "occurred_at": "2026-11-07T14:07:17.759748Z", "test_only": False}
        card = business_message(event)
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("入库样本过期留档", body)
        self.assertIn("至少 3 条", body)
        self.assertIn("15 分钟合并", body)
        self.assertIn("不是处理成功，也没有自动补回", body)
        self.assertIn("2026-11-07 22:07:17", body)
        self.assertNotIn("已恢复", body)
        self.assertNotIn("private", body)
        probe = {**event, "event_id": "inbox-expiry-test:202611071400", "test_only": True}
        probe_body = json.dumps(business_message(probe), ensure_ascii=False)
        self.assertIn("受控演练", probe_body)
        self.assertIn("未进入正常业务处理", probe_body)
        self.assertNotIn("入库样本过期留档", probe_body)
        for change in (
            {"event_id": "inbox-expiry:202611071405"},
            {"event_id": "inbox-expiry:100:105"},
            {"rows": 0}, {"rows": True}, {"rows": 7},
            {"first_inbox_id": 0}, {"last_inbox_id": 99},
            {"occurred_at": "2026-11-07T14:07:17"},
            {"occurred_at": "invalid"}, {"state": "success"},
            {"payload": "private"},
            {"test_only": "true"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                business_message({**event, **change})

    def test_business_endpoint_requires_private_token(self):
        event = {"event_id": "alarm:5:triggered", "category": "device_alarm", "state": "triggered",
                 "alarm_id": 5, "device_code": "INV-1001", "station_code": "ST-01",
                 "metric": "temperature", "level": "major", "value": 81.0,
                 "threshold": 80.0, "occurred_at": "2026-09-29T12:00:00Z"}
        raw = json.dumps(event).encode()
        Handler.business_token = "private-business-token"
        Handler.webhook_url = "https://open.feishu.cn/test"
        Handler.signing_secret = "private-sign"

        def request(token):
            handler = Handler.__new__(Handler)
            handler.path = "/business"
            handler.headers = {"Content-Length": str(len(raw)), "X-PowerSystem-Token": token}
            handler.rfile = io.BytesIO(raw)
            handler.wfile = io.BytesIO()
            statuses = []
            handler.send_error = lambda code: statuses.append(code)
            handler.send_response = lambda code: statuses.append(code)
            handler.end_headers = lambda: None
            handler.do_POST()
            return statuses

        with patch("app.send") as fake_send:
            self.assertEqual(request("wrong"), [403])
            fake_send.assert_not_called()
            self.assertEqual(request("private-business-token"), [200])
            self.assertIn("设备越限", json.dumps(fake_send.call_args.args[2], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
