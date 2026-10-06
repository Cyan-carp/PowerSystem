"""Checks for the read-only private knowledge-miss report."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/view_remote_knowledge_misses.py"
SPEC = importlib.util.spec_from_file_location("remote_misses_viewer", SCRIPT)
viewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(viewer)


class RemoteMissesViewerTest(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "source": "/private/misses",
            "files": ["misses-20261006.jsonl"],
            "records": [
                {"normalized_question": "如何查看告警？", "question": "如何查看告警？",
                 "at": "2026-10-06T01:00:00Z", "reason": "miss", "web_status": "hit", "request_id": "A"},
                {"normalized_question": "如何查看告警？", "question": "如何查看告警？",
                 "at": "2026-10-06T02:00:00Z", "reason": "miss", "web_status": "not_eligible", "request_id": "B"},
                {"normalized_question": "<img src=x onerror=alert(1)>",
                 "at": "2026-10-06T03:00:00Z", "reason": "miss", "web_status": "hit"},
            ],
            "statuses": {},
        }

    def test_summary_groups_by_normalized_question_and_shows_recent_first(self):
        groups = viewer.summarize(self.payload)
        self.assertEqual([group["count"] for group in groups], [2, 1])
        self.assertEqual(groups[0]["last_seen"], "2026-10-06T02:00:00Z")
        self.assertEqual([row["request_id"] for row in groups[0]["records"]], ["B", "A"])
        self.assertEqual(groups[0]["review"]["status"], "pending")

    def test_html_escapes_record_text_and_has_local_only_policy(self):
        report = viewer.render(self.payload, "2026-10-06T04:00:00Z")
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", report)
        self.assertNotIn("<img src=x onerror=alert(1)>", report)
        self.assertIn("connect-src 'none'", report)
        self.assertIn("查看逐条记录（2）", report)

    def test_failed_connection_removes_stale_report(self):
        report = Mock()
        with patch.object(viewer, "REPORT", report), patch.object(viewer, "fetch", side_effect=RuntimeError("offline")):
            code = viewer.main(["--host", "example.invalid", "--port", "7070", "--user", "root",
                                "--identity", "unused", "--no-open"])
        self.assertEqual(code, 1)
        report.unlink.assert_called_once_with(missing_ok=True)

    def test_remote_reader_rejects_invalid_jsonl_without_partial_result(self):
        directory = Path(__file__).parent / "fixtures/invalid_remote_misses"
        mount = [{"Source": str(directory), "Destination": "/knowledge-misses", "Type": "bind"}]
        inspected = SimpleNamespace(stdout=json.dumps(mount))
        with patch("subprocess.run", return_value=inspected), patch.object(sys, "argv", ["-", "agent"]):
            with self.assertRaisesRegex(RuntimeError, "invalid JSONL at misses-20261006.jsonl:2"):
                exec(viewer.REMOTE_READER, {"__name__": "__main__"})


if __name__ == "__main__":
    unittest.main()
