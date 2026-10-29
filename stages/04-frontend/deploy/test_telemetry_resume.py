import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_telemetry_maintenance import stack


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root/'runtime/production-stack'
        self.candidate = self.state/'releases/tr-s04-20261028-r3'
        self.previous = self.state/'releases/parent'
        self.candidate.mkdir(parents=True); self.previous.mkdir()
        stack.write(self.candidate/'private/compose.json', {})
        self.backups = self.root/'backups'; self.backups.mkdir()
        self.archive = self.backups/'fixture.gpg'; self.archive.write_bytes(b'fixture-encrypted-backup')
        self.events = []; self.freeze = {'enabled': True, 'version': 'freeze-v1'}
        self.current_gate = copy.deepcopy(self.freeze)
        self.rows = {n: {'Id': n+'-r3', 'Image': 'sha256:'+n, 'RestartCount': 0,
            'State': {'Running': n not in ('api','gateway'), 'OOMKilled': False, 'StartedAt': 'fixed'},
            'Config': {'Labels': {'com.docker.compose.project': stack.PROJECT,
                'com.docker.compose.service': n,
                'com.docker.compose.project.config_files': str(self.candidate/'private/compose.json')}}}
            for n in ('api','gateway','postgres','prometheus','alertmanager')}
        self.manifest = {'parent': 'parent', 'origin_environment': 'fixture.env',
                         'images': {n:r['Image'] for n,r in self.rows.items()}}
        self.transaction = {'previous': 'parent', 'candidate': self.candidate.name,
            'services': ['api','gateway','prometheus','alertmanager'], 'backup_tier': 'full',
            'predeployment_backup': self.archive.name, 'backup_sha256': stack.sha(self.archive)}
        self.pending = self.state/'pending.json'; stack.write(self.pending, self.transaction)
        self.recovery = self.state/'telemetry-forward-recovery-required.json'
        stack.write(self.recovery, {'phase':'frozen','maintenance_enabled':True,'legacy_worker_started':False,'errors':{}})
        stack.write(self.state/'active.json', {'release': 'parent'})
        stack.write(self.candidate/'telemetry-transition-state.json', {'phase':'ready_for_workers'})
        self.evidence = self.root/'runtime/resume-proof.json'
        self.proof = {'format':'telemetry-same-candidate-resume/v1','candidate':self.candidate.name,
            'attempt':'r1','manifest_sha256':stack.digest(self.manifest),
            'pending_sha256':stack.sha(self.pending),'recovery_sha256':stack.sha(self.recovery),
            'active':stack.read(self.state/'active.json'),'owner_instruction':'继续完成任务',
            'states':stack.states(self.rows),'freeze_gate':self.freeze,
            'tools':{n:stack.sha(Path(stack.__file__).with_name(n)) for n in
                     ('production-stack.py','telemetry-monitoring.py')}}
        stack.write(self.evidence, self.proof)
        class Protocol:
            TelemetryBlocked = RuntimeError
        self.protocol = Protocol()
        self.protocol.schema = lambda *a: {'s04':True}
        self.protocol.gate = lambda *a: self.current_gate
        self.protocol.stop_owned_migrator = lambda *a: {'stop_proven':True}
        def change_gate(_ps,_rows,expected,value):
            self.assertEqual(self.current_gate, expected)
            self.current_gate = {'enabled':value,'version':'restored'}
            self.events.append('release_owned_gate'); return self.current_gate
        self.protocol.change_gate = change_gate
        def halt(*a):
            self.events.append('freeze'); self.current_gate = self.freeze
            for n in ('api','gateway'):self.rows[n]['State']['Running']=False
            raise RuntimeError('frozen')
        self.protocol.halt_forward_recovery = halt
        self.monitor = SimpleNamespace(create_silence=lambda *a:self.events.append('silence'),
            cleanup_silence=lambda *a,**kw:self.events.append('silence_cleanup'))
        self.fail_gate = False
        def deploy(candidate,services,startup_seconds):
            self.assertEqual(candidate, self.candidate); self.assertEqual(startup_seconds,60)
            self.assertFalse(self.current_gate['enabled']); self.events.append('deploy_same_candidate')
            for n in ('api','gateway'):self.rows[n]['State']['Running']=True
        def gate(*a):
            self.events.append('full_gate')
            if self.fail_gate:raise stack.Blocked('fixture_gate_failure')
            return 'accepted-full-https-wss'
        self.patches = patch.multiple(stack, ROOT=self.root, STATE_DIR=self.state, BACKUP_DIR=self.backups,
            release=lambda name:self.state/'releases'/name, immutable=lambda p:self.manifest,
            active=lambda:(self.previous,{}), inspect=lambda:self.rows,
            telemetry_protocol=lambda:self.protocol, telemetry_monitoring=lambda:self.monitor,
            telemetry_transition=lambda:SimpleNamespace(frozen=lambda *a:{'failure_policy':'freeze','source_sha256':'source'}),
            maintenance_receipt=lambda *a:{'bound':True}, command=lambda *a,**kw:self.events.append('source_verify'),
            preflight=lambda *a:self.events.append('preflight'), drain=lambda:self.events.append('drain'),
            deploy=deploy, gate=gate, set_active=lambda *a:self.events.append('activate'))
        self.patches.start(); self.addCleanup(self.patches.stop)
        self.args = SimpleNamespace(id=self.candidate.name,attempt='r1',evidence=str(self.evidence))

    def test_same_candidate_full_gate_before_activation_and_pending_removal(self):
        stack.resume_frozen_telemetry(self.args)
        self.assertFalse(self.pending.exists())
        self.assertLess(self.events.index('full_gate'),self.events.index('activate'))
        self.assertTrue(stack.read(self.candidate/'resume-r1.json')['passed'])
        self.assertTrue((self.candidate/'pending-before-resume-r1.json').exists())
        self.assertTrue((self.candidate/'forward-recovery-before-resume-r1.json').exists())

    def test_failed_gate_refreezes_without_old_worker_or_pending_loss(self):
        self.fail_gate = True; original = self.pending.read_bytes()
        with self.assertRaisesRegex(stack.Blocked,'resume_failed_frozen'):
            stack.resume_frozen_telemetry(self.args)
        self.assertEqual(original,self.pending.read_bytes()); self.assertNotIn('activate',self.events)
        self.assertIn('freeze',self.events)
        self.assertFalse(self.rows['api']['State']['Running'])
        self.assertFalse(stack.read(self.candidate/'resume-r1.json')['passed'])

    def test_operator_gate_change_refuses_before_starting_anything(self):
        self.current_gate = {'enabled':True,'version':'operator-new'}
        with self.assertRaisesRegex(stack.Blocked,'ownership_changed'):
            stack.resume_frozen_telemetry(self.args)
        self.assertNotIn('silence',self.events); self.assertTrue(self.pending.exists())

    def test_pending_tamper_is_rejected(self):
        self.pending.write_bytes(self.pending.read_bytes()+b' ')
        with self.assertRaisesRegex(stack.Blocked,'proof_unbound'):
            stack.resume_frozen_telemetry(self.args)
        self.assertFalse(self.events)

    def test_legacy_or_running_writer_is_refused(self):
        self.rows['api']['State']['Running']=True
        with self.assertRaisesRegex(stack.Blocked,'identity_changed'):
            stack.resume_frozen_telemetry(self.args)
        self.assertNotIn('deploy_same_candidate',self.events)


if __name__ == '__main__':unittest.main()
