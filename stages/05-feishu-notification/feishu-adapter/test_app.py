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
        self.assertIn("设备越限 触发", business_message(event))
        event.update(event_id="alarm:42:acknowledged", state="acknowledged")
        self.assertIn("已确认", business_message(event))
        event.update(event_id="alarm:42:recovered", state="recovered", category="prediction_risk", metric="ai_failure_risk")
        self.assertIn("AI 故障风险 恢复", business_message(event))
        event["event_id"] = "alarm:41:recovered"
        with self.assertRaises(ValueError):
            business_message(event)

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
            self.assertIn("设备越限 触发", fake_send.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
