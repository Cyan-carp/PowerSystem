import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_telemetry_maintenance import FakeBackend, load, stack, tm

transition = load('telemetry-transition')


class Backend(FakeBackend):
    def __init__(self, root):
        super().__init__(root, installed=False)
        self.ROOT = Path(root)
        self.require = stack.require
        self.sha = stack.sha
        self.now = stack.now
        self.compose = lambda p: ['docker', 'compose']
        self.telemetry_protocol = lambda: tm
        self.telemetry_context = lambda: self
        self.telemetry_monitoring = lambda: type('Monitor', (), {'create_silence': staticmethod(lambda ps, candidate, document: ps.write(candidate/'telemetry-planned-silence.json', {'fixture': True}))})
        self.age = 1
        self.fail_migration = False
        self.migrator_running = False
        self.migrator_wrong_image = False

    def command(self, args, log=None, timeout=None):
        if args[:2] == ['docker', 'exec'] and 'oldest_age_seconds' in args[-1]:
            return json.dumps({'oldest_age_seconds': self.age}).encode()
        if args[:2] == ['docker', 'compose']:
            self.events.append(args)
            assert all(not self.rows[n]['State']['Running'] for n in ('api', 'gateway'))
            assert self.gate['enabled'] and '-migrate-only' in args and '-migration-timeout=900s' in args
            assert timeout == 930 and '--no-deps' in args and '--pull' in args
            if self.fail_migration:
                self.migrator_running = True
                raise stack.Blocked('command_timeout:docker')
            self.installed = True
            return b'migration_complete'
        if args[:3] == ['docker', 'ps', '-aq']:
            return b'owned-migrator' if self.migrator_running else b''
        if args[:2] == ['docker', 'inspect']:
            return json.dumps([{'Image': 'foreign-image' if self.migrator_wrong_image else 'candidate-image',
                'Config': {'Labels': {'com.docker.compose.project': 'powersystem-stage4',
                'com.docker.compose.service': 'api', 'com.docker.compose.oneoff': 'True'}}}]).encode()
        if args[:2] == ['docker', 'stop'] and args[-1] == 'owned-migrator':
            self.migrator_running = False
            return b''
        if args[:3] == ['docker', 'ps', '-q']:
            return b'owned-migrator' if self.migrator_running else b''
        return super().command(args)


class TelemetryTransitionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ps = Backend(self.directory.name)
        self.candidate = Path(self.directory.name)/'tr-s04-20261028-r2'
        self.candidate.mkdir()
        source = self.ps.ROOT/'runtime/source-builds/contexts/source-hash'
        source.mkdir(parents=True)
        self.ps.write(source/'source-manifest.json', {'files': {
            'stages/02-backend/cmd/api/main.go': {'sha256': 'entry-hash'},
            'stages/02-backend/deploy/postgres/016_telemetry_realtime.sql': {'sha256': 'sql'},
            'stages/04-frontend/deploy/telemetry-maintenance.py': {'sha256': 'protocol-hash'}}})
        self.builds = [{'service': service, 'source_sha256': 'source-hash'} for service in ('api', 'gateway')]
        self.document = {'format': 1, 'kind': 'telemetry-forward-only/v1', 'parent': 'parent',
            'candidate': self.candidate.name,
            'failure_policy': 'freeze', 'owner_accepted_freeze_on_failure': True,
            'owner_instruction': '失败时冻结遥测写入、保留现场，修复后恢复',
            'migration_timeout_seconds': 900, 'source_sha256': 'source-hash',
            'schema_entry_sha256': 'entry-hash', 'protocol_helper_sha256': 'protocol-hash',
            'large_table_evidence': {'passed': True, 'target_rows': 14653886, 'candidate_changed': [],
                'offline_15m': {'exit': 0, 'seconds': 101}, 'repeat_default_30s': {'exit': 0},
                'before': {'rows': 14653886}, 'after': {'rows': 14653886}, 'legacy_null_count': 14653886},
            'protocol_evidence': {'passed': True, 'source_changed': [], 'real_gateway_linux_sigterm': True,
                'maintenance_receipt_valid': True, 'legacy_worker_started': False, 'pending_retained': True}}
        self.ps.write(self.candidate/'telemetry-transition.json', self.document)
        h = self.ps.sha(self.candidate/'telemetry-transition.json')
        self.manifest = {'parent': 'parent', 'services': ['api', 'gateway', 'prometheus', 'alertmanager'], 'backup_tier': 'full',
            'source_builds': self.builds, 'files': {'telemetry-transition.json': h},
            'telemetry_transition': {'sha256': h}, 'images': {'api': 'candidate-image'}}

    def validate(self, document=None, services=None, tier='full', builds=None):
        return transition.validate(self.ps, document or self.document, 'parent',
            services or ['api', 'gateway', 'prometheus', 'alertmanager'], tier, builds or self.builds)

    def test_owner_choice_and_full_paired_scope_are_required(self):
        self.validate()
        for key, value in [('owner_accepted_freeze_on_failure', False), ('failure_policy', 'rollback'),
                           ('migration_timeout_seconds', 3600), ('parent', 'other'),
                           ('candidate', 'unreviewed-release'),
                           ('schema_entry_sha256', 'other'), ('protocol_helper_sha256', 'other')]:
            d = copy.deepcopy(self.document);d[key] = value
            with self.subTest(key=key), self.assertRaises(stack.Blocked):self.validate(d)
        with self.assertRaises(stack.Blocked):self.validate(tier='frontend-fast')
        with self.assertRaises(stack.Blocked):self.validate(services=['api'])

    def test_failed_or_changed_evidence_is_not_a_pass(self):
        for item, key, value in [('large_table_evidence', 'passed', False),
                                 ('large_table_evidence', 'candidate_changed', ['main.go']),
                                 ('large_table_evidence', 'before', None),
                                 ('protocol_evidence', 'legacy_worker_started', True),
                                 ('protocol_evidence', 'pending_retained', False)]:
            d = copy.deepcopy(self.document);d[item][key] = value
            with self.subTest(key=key), self.assertRaises(stack.Blocked):self.validate(d)

    def test_frozen_proof_cannot_be_substituted(self):
        transition.frozen(self.ps, self.candidate, self.manifest)
        p = self.candidate/'telemetry-transition.json'
        p.write_text(p.read_text()+' ')
        with self.assertRaisesRegex(stack.Blocked, 'frozen_binding_changed'):
            transition.frozen(self.ps, self.candidate, self.manifest)

    def test_unstaged_schema_cannot_enter_ordinary_publish(self):
        with self.assertRaisesRegex(stack.Blocked, 'offline_transition_required'):
            transition.require_if_needed(self.ps, self.builds, None)
        self.assertFalse(any(x[:2] == ['docker', 'stop'] for x in self.ps.events))
        self.ps.installed = True
        transition.require_if_needed(self.ps, self.builds, None)

    def test_existing_history_is_refused_before_any_stop(self):
        self.ps.age = 16
        with self.assertRaisesRegex(stack.Blocked, 'backlog_requires_authorization'):
            transition.apply(self.ps, self.candidate, self.manifest, {})
        self.assertFalse(any(x[:2] == ['docker', 'stop'] for x in self.ps.events))

    def test_offline_migration_only_after_both_old_workers_stopped(self):
        transition.apply(self.ps, self.candidate, self.manifest, {})
        value = self.ps.read(self.candidate/'telemetry-transition-state.json')
        self.assertEqual(value['phase'], 'ready_for_workers')
        self.assertFalse(self.ps.gate['enabled'])
        self.assertTrue(self.ps.installed)
        self.assertTrue(all(not self.ps.rows[n]['State']['Running'] for n in ('api', 'gateway')))

    def test_timeout_stops_only_owned_migrator_before_old_schema_recovery(self):
        self.ps.fail_migration = True
        with self.assertRaises(stack.Blocked):transition.apply(self.ps, self.candidate, self.manifest, {})
        self.assertFalse(self.ps.migrator_running)
        self.assertEqual(self.ps.read(self.candidate/'telemetry-transition-state.json')['phase'], 'migration_failed_stopped')
        transition.restore_pre_schema_gate(self.ps, self.candidate)
        self.assertFalse(self.ps.gate['enabled'])

    def test_foreign_migrator_is_not_stopped_or_rolled_back_over(self):
        self.ps.fail_migration = self.ps.migrator_wrong_image = True
        with self.assertRaises(stack.Blocked):transition.apply(self.ps, self.candidate, self.manifest, {})
        self.assertTrue(self.ps.migrator_running)
        self.assertEqual(self.ps.read(self.candidate/'telemetry-transition-state.json')['phase'], 'migration_stop_unproven')
        with self.assertRaisesRegex(stack.Blocked, 'manual_recovery'):
            transition.restore_pre_schema_gate(self.ps, self.candidate)
        self.assertTrue(self.ps.gate['enabled'])

    def test_non_telemetry_rollback_does_not_probe_a_database(self):
        parent = Path(self.directory.name)/'legacy';parent.mkdir()
        self.ps.release = lambda name: parent
        self.ps.write(parent/'manifest.json', {'source_builds': []})
        self.assertFalse(transition.needs_schema_guard(self.ps, {'candidate': 'legacy', 'services': ['api']}))
        self.assertFalse(transition.needs_schema_guard(self.ps, {'candidate': 'legacy', 'services': ['frontend']}))
        self.assertFalse(self.ps.events)

    def test_compatible_schema_recovery_restores_only_owned_gate(self):
        self.ps.fail_migration = True
        with self.assertRaises(stack.Blocked):transition.apply(self.ps, self.candidate, self.manifest, {})
        self.ps.installed = True
        transition.ensure_migrator_stopped(self.ps, self.candidate)
        transition.restore_pre_schema_gate(self.ps, self.candidate)
        self.assertFalse(self.ps.gate['enabled'])


if __name__ == '__main__':unittest.main()
