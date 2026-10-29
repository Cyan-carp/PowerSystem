import copy
import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


def load(name):
    path = Path(__file__).with_name(name+'.py')
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tm = load('telemetry-maintenance')
stack = load('production-stack')


class FakeBackend:
    def __init__(self, root, installed=True):
        self.STATE_DIR = Path(root)
        self.BACKUP_DIR = Path(root)
        self.installed = installed
        self.events = []
        self.gate = {'enabled': False, 'version': 'v0'}
        self.serial = 0
        self.capable_parent = False
        self.rows = {n: {'Id': n+'-id', 'Image': n+'-image', 'RestartCount': 0,
                        'State': {'Running': True, 'ExitCode': 0, 'OOMKilled': False,
                                  'StartedAt': '2026-01-01T00:00:00Z'}}
                     for n in ('api', 'gateway', 'postgres')}
        self.write(self.STATE_DIR/'active.json', {'release': 'fixture'})

    def read(self, path):
        return json.loads(Path(path).read_text())

    def write(self, path, value):
        Path(path).write_text(json.dumps(value))

    def inspect(self):
        return copy.deepcopy(self.rows)

    def healthy(self, rows):
        assert all(r['State']['Running'] for r in rows.values())

    def states(self, rows):
        return {n: {'id': r['Id'], 'image': r['Image'], 'restarts': r['RestartCount'],
                    'started_at': r['State']['StartedAt']} for n, r in rows.items()}

    def command(self, args, log=None, timeout=None):
        self.events.append(args)
        if args[:2] == ['docker', 'exec']:
            q = args[-1]
            if 'information_schema.columns' in q:
                value = {'s01': self.installed, 's04': self.installed}
            elif 'UPDATE alarm_maintenance_state' in q:
                if "updated_at='"+self.gate['version']+"'" not in q:
                    return b''
                self.serial += 1
                self.gate = {'enabled': 'enabled=TRUE' in q, 'version': 'v'+str(self.serial)}
                value = self.gate
            else:
                value = self.gate
            return json.dumps(value).encode()
        if args[:2] == ['docker', 'stop']:
            assert self.gate['enabled'], 'gateway stopped before the drain barrier'
            row = next(r for r in self.rows.values() if r['Id'] == args[-1])
            row['State']['Running'] = False
            return b''
        if args[:2] == ['docker', 'start']:
            assert self.gate['enabled'], 'gate released before writer restart'
            self.rows['gateway']['State']['Running'] = True
            self.rows['gateway']['State']['StartedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return b''
        if args[:2] == ['docker', 'run']:
            assert args[args.index('--entrypoint')+1] == 'sh'
            if not self.capable_parent:
                raise RuntimeError('historical image lacks telemetry contract')
            return b''
        raise AssertionError(args)


class TelemetryMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ps = FakeBackend(self.directory.name)
        self.path = Path(self.directory.name)/'telemetry-backup-state.json'

    def test_backup_drains_before_stop_and_restores_only_owned_gate(self):
        before = self.ps.states(self.ps.inspect())
        tm.pause(self.ps, self.path)
        self.assertTrue(self.ps.gate['enabled'])
        self.assertFalse(self.ps.rows['gateway']['State']['Running'])
        tm.resume(self.ps, self.path)
        self.assertFalse(self.ps.gate['enabled'])
        after = self.ps.states(self.ps.inspect())
        value = tm.receipt(self.ps, self.path)
        self.assertTrue(tm.validate_receipt(before, after, value))
        stack.maintenance_states_match(before, after, value)
        with self.assertRaises(stack.Blocked):
            stack.maintenance_states_match(before, after)
        count = len(self.ps.events)
        tm.resume(self.ps, self.path)
        self.assertEqual(count, len(self.ps.events), 'resume replay changed state')

    def test_operator_later_gate_change_is_never_undone(self):
        tm.pause(self.ps, self.path)
        self.ps.gate['version'] = 'operator-new-version'
        with self.assertRaisesRegex(tm.TelemetryBlocked, 'ownership_changed'):
            tm.resume(self.ps, self.path)
        self.assertTrue(self.ps.gate['enabled'])
        self.assertFalse(self.ps.rows['gateway']['State']['Running'])

    def test_gateway_replacement_does_not_get_started(self):
        tm.pause(self.ps, self.path)
        self.ps.rows['gateway']['Id'] = 'another-session'
        with self.assertRaisesRegex(tm.TelemetryBlocked, 'identity_changed'):
            tm.resume(self.ps, self.path)
        self.assertFalse(any(x[:2] == ['docker', 'start'] for x in self.ps.events))

    def test_sigkill_is_not_signed_as_drained(self):
        original = self.ps.command

        def command(args, **kwargs):
            value = original(args, **kwargs)
            if args[:2] == ['docker', 'stop']:
                self.ps.rows['gateway']['State']['ExitCode'] = 137
            return value

        self.ps.command = command
        with self.assertRaisesRegex(tm.TelemetryBlocked, 'not_cleanly_drained'):
            tm.pause(self.ps, self.path)
        with self.assertRaisesRegex(tm.TelemetryBlocked, 'not_cleanly_drained'):
            tm.resume(self.ps, self.path)
        with self.assertRaises(tm.TelemetryBlocked):
            tm.receipt(self.ps, self.path)

    def test_prior_maintenance_is_preserved(self):
        self.ps.gate['enabled'] = True
        tm.pause(self.ps, self.path)
        tm.resume(self.ps, self.path)
        self.assertTrue(self.ps.gate['enabled'])

    def test_old_schema_backup_does_not_stop_gateway(self):
        self.ps.installed = False
        tm.pause(self.ps, self.path)
        tm.resume(self.ps, self.path)
        self.assertIsNone(tm.receipt(self.ps, self.path))
        self.assertFalse(any(x[:2] == ['docker', 'stop'] for x in self.ps.events))

    def test_forward_schema_blocks_legacy_worker_before_rollback(self):
        previous = Path(self.directory.name)
        self.ps.write(previous/'manifest.json', {'images': {'api': 'legacy-image'}})
        self.ps.write(previous/'pending.json', {'candidate': 'new'})
        with self.assertRaisesRegex(tm.TelemetryBlocked, 'requires_forward_recovery'):
            tm.refuse_unsafe_parent(self.ps, previous, {'candidate': 'new'})
        self.assertTrue(self.ps.gate['enabled'])
        self.assertFalse(self.ps.rows['api']['State']['Running'])
        self.assertFalse(self.ps.rows['gateway']['State']['Running'])
        self.assertTrue((previous/'pending.json').exists())
        self.assertFalse(self.ps.read(previous/'telemetry-forward-recovery-required.json')['legacy_worker_started'])

    def test_explicit_unsafe_rollback_does_not_stop_healthy_candidate(self):
        previous = Path(self.directory.name)
        self.ps.write(previous/'manifest.json', {'images': {'api': 'legacy-image'}})
        with self.assertRaisesRegex(tm.TelemetryBlocked, 'legacy_parent_incompatible'):
            tm.refuse_unsafe_parent(self.ps, previous, {'kind': 'explicit_parent_rollback'})
        self.assertTrue(self.ps.rows['api']['State']['Running'])

    def test_stop_failure_still_attempts_other_writer_and_retains_evidence(self):
        original = self.ps.command
        def command(args, **kwargs):
            if args[:2] == ['docker', 'stop'] and args[-1] == 'api-id':
                raise RuntimeError('stop timeout')
            return original(args, **kwargs)
        self.ps.command = command
        with self.assertRaises(tm.TelemetryBlocked):
            tm.halt_forward_recovery(self.ps, self.ps.inspect(), {'candidate': 'new'}, {'s04': True}, 'forward')
        evidence = self.ps.read(self.ps.STATE_DIR/'telemetry-forward-recovery-required.json')
        self.assertEqual(evidence['stopped'], ['gateway'])
        self.assertEqual(evidence['errors']['api'], 'stop_unproven')
        self.assertEqual(evidence['phase'], 'recovery_incomplete')
        self.assertFalse(evidence['passed'])

    def test_sql_file_presence_does_not_prove_compatible_parent(self):
        previous = self.ps.STATE_DIR
        self.ps.write(previous/'manifest.json', {'images': {'api': 'new', 'gateway': 'new'}, 'parent': None})
        self.assertFalse(tm.compatible_parent(self.ps, previous, {'s01': True, 's04': True}))
        self.assertFalse(tm.compatible_parent(self.ps, previous, {'s01': True, 's04': False}))

    def test_failed_backup_resume_runs_full_gate_and_never_claims_backup_success(self):
        root = self.ps.STATE_DIR
        receipt_path = root/'powersystem-20261028T140637Z.tar.gz.gpg.v4-maintenance.json'
        timing_path = root/'powersystem-20261028T140637Z.timing.json'
        pointer = {'release': 'fixture', 'gate_file': 'old', 'published_at': 'original'}
        states = self.ps.states(self.ps.inspect())
        self.ps.write(root/'active.json', pointer)
        self.ps.write(root/'old', {'states': states})
        self.ps.write(receipt_path, {'archive': None, 'version': 1, 'active': pointer,
                     'before': states, 'after': states, 'resumed_at': stack.now()})
        timing = {'status': 'failed', 'archive_bytes': 0, 'uploads': [], 'failed_stage': 'queues_snapshot'}
        self.ps.write(timing_path, timing)
        with patch.multiple(stack, STATE_DIR=root, BACKUP_DIR=root, active=lambda: (root, {}),
             inspect=self.ps.inspect, states=self.ps.states,
             gate=lambda *a: 'new-full-https-wss',
             write=unittest.mock.DEFAULT, set_active=unittest.mock.DEFAULT) as mocks:
            stack.verify_failed_backup_resume(SimpleNamespace(receipt=str(receipt_path)))
            self.assertFalse(mocks['write'].call_args.args[1]['backup_passed'])
            self.assertEqual(mocks['set_active'].call_args.args[-1], 'original')
            timing['uploads'] = [{'name': 'upload_primary'}]
            self.ps.write(timing_path, timing)
            with self.assertRaisesRegex(stack.Blocked, 'resume_not_proven'):
                stack.verify_failed_backup_resume(SimpleNamespace(receipt=str(receipt_path)))
            with self.assertRaisesRegex(stack.Blocked, 'receipt_path_invalid'):
                stack.verify_failed_backup_resume(SimpleNamespace(receipt=str(root/'outside'/'receipt.json')))


if __name__ == '__main__':
    unittest.main()
