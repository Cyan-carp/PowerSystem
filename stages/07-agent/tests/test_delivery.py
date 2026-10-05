import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('observe', ROOT / 'deploy/observe.py')
observe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observe)
spec = importlib.util.spec_from_file_location('ledger', ROOT / 'scripts/v2m4-evidence.py')
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)


class DeliveryLedgerTest(unittest.TestCase):
    def test_missing_skipped_interrupted_and_tampered_evidence_fail(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); file=root/'proof.json';file.write_text('{}')
            digest=hashlib.sha256(file.read_bytes()).hexdigest()
            rows=[{'gate':gate,'result':'passed','evidence':[{'path':file.name,'sha256':digest}]} for gate in ledger.REQUIRED]
            self.assertTrue(ledger.evaluate(root,rows))
            self.assertFalse(ledger.evaluate(root,rows[:-1]))
            for status in ('skipped','interrupted','failed'):
                changed=copy.deepcopy(rows);changed[0]['result']=status
                self.assertFalse(ledger.evaluate(root,changed))
            file.write_text('{"changed":true}')
            self.assertFalse(ledger.evaluate(root,rows))


class ObservationTest(unittest.TestCase):
    def baseline(self):
        return {s: {'running': True, 'health': 'healthy', 'image': 'sha256:' + s, 'restart_count': 0}
                for s in observe.SERVICES}

    def test_image_drift_restart_and_missing_service_fail(self):
        baseline = self.baseline()
        self.assertTrue(observe.service_checks(baseline, baseline))
        for field, value in [('image', 'different'), ('restart_count', 1), ('running', False), ('health', 'unhealthy')]:
            changed = copy.deepcopy(baseline); changed['agent'][field] = value
            self.assertFalse(observe.service_checks(changed, baseline))
        changed = copy.deepcopy(baseline); del changed['frontend']
        self.assertFalse(observe.service_checks(changed, baseline))

    def test_complete_run_and_non_overwrite(self):
        clock = [0]
        def sleep(value): clock[0] += value
        def request(url, path): return {'code': 200 if path.endswith('ping') else 401 if '/api/v1/' in path else 403}
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'observation.json'
            self.assertTrue(observe.main(1800, target, 'https://example.test', inspector=self.baseline,
                requester=request, monotonic=lambda: clock[0], sleep=sleep))
            report = json.loads(target.read_text())
            self.assertTrue(report['completed']); self.assertEqual(report['elapsed_seconds'], 1800)
            self.assertGreaterEqual(len(report['samples']), 60)
            with self.assertRaises(FileExistsError): observe.main(1800, target, 'https://example.test')

    def test_wrong_release_and_interruption_are_not_success(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'wrong.json'
            with self.assertRaises(ValueError): observe.main(1800, target, 'https://example.test',
                {s:'wrong' for s in observe.SERVICES}, inspector=self.baseline)
            self.assertFalse(json.loads(target.read_text())['passed'])
            target = Path(d) / 'interrupted.json'
            def request(url,path): raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt): observe.main(1800,target,'https://example.test',
                inspector=self.baseline,requester=request)
            report=json.loads(target.read_text());self.assertFalse(report['passed']);self.assertFalse(report['completed'])
