import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("entry_probe", Path(__file__).with_name("entry-probe.py"))
entry_probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry_probe)


class EntryCardTests(unittest.TestCase):
    def test_failure_and_recovery_are_structured_cards(self):
        failure = entry_probe.entry_card(False, ["api: HTTP 503（连续 2 次）", "web: 200"], "检查入口。")
        self.assertEqual(failure["header"]["template"], "red")
        body = json.dumps(failure, ensure_ascii=False)
        self.assertIn("api: HTTP 503", body)
        self.assertIn("处理建议", body)
        recovery = entry_probe.entry_card(True, ["api: 200", "web: 200"], "已经恢复。")
        self.assertEqual(recovery["header"]["template"], "green")
        self.assertIn("已恢复", json.dumps(recovery, ensure_ascii=False))

    def test_notification_sends_interactive_card_without_real_webhook(self):
        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, *_):
                return b'{"code":0}'

        captured = {}

        class Opener:
            def open(self, request, timeout):
                captured.update(json.loads(request.data))
                return Reply()

        with patch.object(entry_probe, "opener", return_value=Opener()), \
             patch.object(entry_probe.Path, "read_text", side_effect=["https://open.feishu.cn/test", "signing-key"]):
            self.assertTrue(entry_probe.notify(entry_probe.entry_card(False, ["api: HTTP 503"], "检查入口。")))
        self.assertEqual(captured["msg_type"], "interactive")
        self.assertEqual(captured["card"]["header"]["template"], "red")


if __name__ == "__main__":
    unittest.main()
