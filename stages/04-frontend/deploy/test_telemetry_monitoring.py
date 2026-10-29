from pathlib import Path
import io
import json
import tempfile
import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_telemetry_maintenance import load, stack

monitoring = load('telemetry-monitoring')


class TelemetryMonitoringTests(unittest.TestCase):
    def test_only_new_telemetry_alerts_are_routed_to_an_empty_receiver(self):
        original = (Path(__file__).parents[2]/'07-agent/deploy/alertmanager-agent.yml').read_text(encoding='utf-8')
        rendered = monitoring.render_alertmanager(original)
        self.assertLess(rendered.index('receiver: telemetry-local-only'), rendered.index('receiver: agent'))
        self.assertIn('component="telemetry"', rendered)
        self.assertIn('alertname=~"PowerSystemTelemetry.*"', rendered)
        empty = rendered.split('  - name: telemetry-local-only\n', 1)[1]
        self.assertTrue(empty.startswith('  - name: feishu\n'), 'empty receiver acquired outbound configuration')
        self.assertEqual(original.count('webhook_configs:'), rendered.count('webhook_configs:'))
        self.assertIn('credentials_file: /run/secrets/agent_monitor_token', rendered)
        self.assertIn('PowerSystemAPIDown|PowerSystemDiskHigh', rendered)
        self.assertEqual(original[original.index('    - receiver: agent'):original.index('\nreceivers:')],
                         rendered[rendered.index('    - receiver: agent'):rendered.index('\nreceivers:')])

    def test_unknown_or_already_converted_parent_is_not_guessed(self):
        with self.assertRaises(ValueError):monitoring.render_alertmanager('route:\n  receiver: custom\nreceivers: []\n')
        original = (Path(__file__).parents[2]/'07-agent/deploy/alertmanager-agent.yml').read_text(encoding='utf-8')
        with self.assertRaises(ValueError):monitoring.render_alertmanager(monitoring.render_alertmanager(original))

    def test_an_ambiguous_management_network_is_refused(self):
        with self.assertRaises(ValueError):
            monitoring.endpoint({'alertmanager': {'NetworkSettings': {'Networks': {
                'a': {'IPAddress': '127.0.0.1'}, 'b': {'IPAddress': '127.0.0.2'}}}}})

    def _exercise_silence(self, attempt='', changed_expiry=False):
        with tempfile.TemporaryDirectory() as root:
            candidate = Path(root)/'tr-s04-20261028-r2';candidate.mkdir()
            stack.write(candidate/'telemetry-transition.json', {'source_sha256': 'source'})
            receipt_path = monitoring.silence_file(candidate, attempt)
            if attempt:
                stack.write(candidate/'telemetry-planned-silence.json', {'original_evidence':'retained'})
            rows = {s: {'NetworkSettings': {'Networks': {'test': {'IPAddress': '127.0.0.1'}}}}
                    for s in ('prometheus', 'alertmanager')}
            ps = SimpleNamespace(read=stack.read, write=stack.write, require=stack.require, sha=stack.sha,
                                 Blocked=stack.Blocked, inspect=lambda: rows)
            stored = {}; calls = []
            def urlopen(request, timeout):
                url = request if isinstance(request, str) else request.full_url
                method = 'GET' if isinstance(request, str) else request.get_method()
                calls.append((url, method))
                if '/targets?' in url:
                    value = {'data': {'activeTargets': [{'labels': {'job': 'api', 'instance': 'api:8080'}}]}}
                elif method == 'POST':
                    stored.update(json.loads(request.data), status={'state': 'active'});value = {'silenceID': 'owned'}
                elif method == 'DELETE':
                    stored.update(status={'state': 'expired'}, endsAt='2026-01-01T00:00:00Z');value = {}
                else:value = stored
                return io.BytesIO(json.dumps(value).encode())
            with patch.object(monitoring.urllib.request, 'urlopen', side_effect=urlopen):
                proof = monitoring.create_silence(ps, candidate, {'source_sha256': 'source'}, attempt)
                # Model a pre-fix receipt with microseconds that Alertmanager
                # persisted only to milliseconds, as observed in production.
                legacy = stack.read(receipt_path)
                legacy['expires_at'] = (datetime.datetime.fromisoformat(legacy['expires_at'])+
                                         datetime.timedelta(microseconds=778)).isoformat()
                stack.write(receipt_path, legacy)
                self.assertIn({'name': 'instance', 'value': 'api:8080', 'isRegex': False, 'isEqual': True}, proof['matchers'])
                self.assertIn({'name': 'job', 'value': 'api', 'isRegex': False, 'isEqual': True}, proof['matchers'])
                if changed_expiry:
                    stored['endsAt'] = (datetime.datetime.fromisoformat(stored['endsAt'])+
                                        datetime.timedelta(milliseconds=1)).isoformat()
                    with self.assertRaises(stack.Blocked):monitoring.cleanup_silence(ps, candidate, attempt=attempt)
                    self.assertFalse(any(method=='DELETE' for _,method in calls))
                    return
                monitoring.cleanup_silence(ps, candidate, attempt=attempt)
                self.assertTrue(stack.read(receipt_path)['removed'])
                self.assertEqual([m for u,m in calls][-2:], ['DELETE', 'GET'])
                self.assertFalse((candidate/('telemetry-silence-recovery-required'+('-'+attempt if attempt else '')+'.json')).exists())
                if attempt:self.assertEqual(stack.read(candidate/'telemetry-planned-silence.json'), {'original_evidence':'retained'})

    def test_silence_is_bound_to_target_source_and_actively_expired(self):
        self._exercise_silence()

    def test_resume_attempt_cleans_only_its_own_silence(self):
        self._exercise_silence('resume-r1')

    def test_cross_millisecond_expiry_change_is_refused(self):
        self._exercise_silence('resume-r1', changed_expiry=True)

    def test_cleanup_outage_is_recorded_and_blocks_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            ps = SimpleNamespace(write=stack.write, Blocked=stack.Blocked)
            with patch.object(monitoring, 'remove_silence', side_effect=OSError('unavailable')):
                with self.assertRaisesRegex(stack.Blocked, 'cleanup_requires_recovery'):
                    monitoring.cleanup_silence(ps, Path(root))
                self.assertEqual(stack.read(Path(root)/'telemetry-silence-recovery-required.json')['attempt'], 2)
                monitoring.cleanup_silence(ps, Path(root), strict=False)
                monitoring.cleanup_silence(ps, Path(root), strict=False, attempt='resume-r1')
                self.assertTrue((Path(root)/'telemetry-silence-recovery-required-resume-r1.json').exists())


if __name__ == '__main__':unittest.main()
