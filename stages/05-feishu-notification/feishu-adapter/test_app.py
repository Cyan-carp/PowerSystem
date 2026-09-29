import unittest
from unittest.mock import patch

from app import message, send, sign


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


if __name__ == "__main__":
    unittest.main()
