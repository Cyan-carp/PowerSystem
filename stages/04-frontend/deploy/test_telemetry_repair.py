import copy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_telemetry_resume as resume_fixture
from test_telemetry_maintenance import load, stack, tm

repair = load('telemetry-repair')


class RepairTests(unittest.TestCase):
    def setUp(self):
        resume_fixture.ResumeTests.setUp(self)
        for row in self.rows.values():row['State']['ExitCode']=0
        self.proof.update(format=repair.FORMAT,candidate=repair.REPAIR,failed_candidate=repair.FAILED,
            failed_manifest_sha256=stack.digest(self.manifest))
        self.ack=self.root/'runtime/ack.json'
        stack.write(self.ack,{'passed':True,'sources_unchanged':True,'api_image':self.manifest['images']['api'],
            'source_sha256':'tested-source','binary_sha256':'a'*64})
        self.proof.update(repair_images={n:self.manifest['images'][n] for n in ('api','gateway')},
            repair_source_sha256='tested-source',ack_evidence={'path':str(self.ack),'sha256':stack.sha(self.ack),'binary_sha256':'a'*64})
        stack.write(self.evidence,self.proof)
        self.addCleanup(patch.stopall)
        patch.object(stack,'telemetry_repair',return_value=repair).start()
        self.repaired=self.state/'releases'/repair.REPAIR;self.repaired.mkdir()
        stack.write(self.repaired/'telemetry-repair.json',self.proof)
        stack.write(self.repaired/'private/compose.json',{})

    def test_frozen_admission_is_read_only_and_binds_all_instances(self):
        pending=self.pending.read_bytes();gate=copy.deepcopy(self.current_gate)
        repair.admit(stack,self.evidence)
        self.assertEqual(pending,self.pending.read_bytes());self.assertEqual(gate,self.current_gate)
        self.assertFalse(self.events)

    def test_operator_gate_change_blocks_before_backup_or_repair(self):
        self.current_gate={'enabled':True,'version':'operator-update'}
        with self.assertRaisesRegex(stack.Blocked,'schema_or_gate_changed'):repair.admit(stack,self.evidence)
        self.assertFalse(self.events)

    def test_changed_pending_or_running_worker_is_refused(self):
        self.rows['api']['State']['Running']=True
        with self.assertRaisesRegex(stack.Blocked,'writer_changed'):repair.admit(stack,self.evidence)
        self.rows['api']['State']['Running']=False
        self.pending.write_bytes(self.pending.read_bytes()+b' ')
        with self.assertRaisesRegex(stack.Blocked,'transaction_changed'):repair.admit(stack,self.evidence)

    def test_non_backup_drift_cannot_be_excused(self):
        after=stack.states(self.rows);after['postgres']['started_at']='different'
        with self.assertRaises(stack.Blocked):repair.admit(stack,self.evidence,states_after=after)

    def test_frozen_backup_preserves_stopped_writers_and_gate(self):
        state=self.backups/'staging';state.mkdir();receipt=state/'v4-maintenance.json'
        self.protocol.receipt=lambda _ps,p:tm.receipt(stack,p)
        with patch.dict(os.environ,{repair.ENV:str(self.evidence)}):
            stack.backup_maintenance_snapshot(receipt)
            tm.pause(stack,state/'telemetry-backup-state.json')
            tm.resume(stack,state/'telemetry-backup-state.json')
            stack.backup_maintenance_snapshot(receipt,after=True)
        result=stack.read(receipt)
        self.assertEqual(result['before'],result['after'])
        self.assertEqual(result['telemetry']['phase'],'preserved_frozen')
        self.assertEqual(result['frozen_repair_sha256'],stack.sha(self.evidence))
        self.assertTrue(self.current_gate['enabled']);self.assertFalse(self.rows['gateway']['State']['Running'])
        self.assertFalse(self.events)

    def publish_fixture(self):
        frozen=self.repaired/'telemetry-repair.json'
        manifest=dict(self.manifest,services=self.transaction['services'],before=self.proof['states'],
            created_at='2026-01-01T00:00:00+00:00',files={frozen.name:stack.sha(frozen)},
            telemetry_repair={'sha256':stack.sha(frozen)},
            source_builds=[{'service':n,'source_sha256':'tested-source'} for n in ('api','gateway')])
        self.repair_manifest=manifest
        old=self.manifest
        patch.object(stack,'immutable',side_effect=lambda path:manifest if path==self.repaired else old).start()
        patch.object(stack,'verified_full_baseline',return_value={'sha256':stack.sha(self.archive)}).start()
        receipt={'archive':self.archive.name,'sha256':stack.sha(self.archive),'frozen_repair_sha256':stack.sha(frozen),
            'repair_candidate':repair.REPAIR,'started_at':'2026-01-02T00:00:00+00:00',
            'active':self.proof['active'],'before':self.proof['states'],'after':self.proof['states'],
            'telemetry':{'kind':repair.FORMAT,'phase':'preserved_frozen'}}
        stack.write(Path(str(self.archive)+'.v4-maintenance.json'),receipt)
        def deploy(*a,**kw):
            self.events.append('deploy_new_candidate')
            self.assertFalse(self.current_gate['enabled'])
            for name in ('api','gateway'):
                self.rows[name]['State']['Running']=True
                self.rows[name]['Config']['Labels']['com.docker.compose.project.config_files']=str(self.repaired/'private/compose.json')
        patch.object(stack,'deploy',side_effect=deploy).start()
        return SimpleNamespace(id=repair.REPAIR,backup=str(self.archive))

    def test_new_candidate_requires_full_gate_and_keeps_failed_transaction_copy(self):
        args=self.publish_fixture();original=self.pending.read_bytes();repair.publish(stack,args)
        self.assertFalse(self.pending.exists())
        self.assertEqual((self.repaired/'previous-pending.json').read_bytes(),original)
        self.assertLess(self.events.index('full_gate'),self.events.index('activate'))
        self.assertTrue(stack.read(self.repaired/'repair-result.json')['passed'])

    def test_failed_new_candidate_refreezes_and_preserves_both_transactions(self):
        args=self.publish_fixture();original=self.pending.read_bytes();self.fail_gate=True
        with self.assertRaisesRegex(stack.Blocked,'repair_failed_frozen'):repair.publish(stack,args)
        self.assertEqual((self.repaired/'previous-pending.json').read_bytes(),original)
        self.assertEqual(stack.read(self.pending)['candidate'],repair.REPAIR)
        self.assertNotIn('activate',self.events);self.assertIn('freeze',self.events)
        self.assertTrue(self.current_gate['enabled']);self.assertFalse(self.rows['api']['State']['Running'])

    def test_failed_r4_can_take_frozen_backup_and_resume_with_fresh_proof(self):
        args=self.publish_fixture();self.fail_gate=True
        with self.assertRaises(stack.Blocked):repair.publish(stack,args)
        fresh=dict(self.proof,failed_candidate=repair.REPAIR,failed_manifest_sha256=stack.digest(self.repair_manifest),
            pending_sha256=stack.sha(self.pending),recovery_sha256=stack.sha(self.recovery),
            states=stack.states(self.rows),freeze_gate=self.current_gate)
        path=self.root/'runtime/fresh-r4-proof.json';stack.write(path,fresh)
        with patch.dict(os.environ,{repair.ENV:str(path)}):repair.backup_context(stack)
        self.fail_gate=False
        resume=dict(fresh,format='telemetry-same-candidate-resume/v1',candidate=repair.REPAIR,
            attempt='r1',manifest_sha256=stack.digest(self.repair_manifest))
        stack.write(path,resume)
        stack.resume_frozen_telemetry(SimpleNamespace(id=repair.REPAIR,attempt='r1',evidence=str(path)))
        self.assertFalse(self.pending.exists());self.assertEqual(stack.read(self.recovery)['phase'],'resolved')

    def test_untested_image_or_changed_ack_receipt_cannot_publish(self):
        args=self.publish_fixture();self.repair_manifest['images']=dict(self.repair_manifest['images'],api='sha256:other')
        with self.assertRaisesRegex(stack.Blocked,'tested_images_unbound'):repair.publish(stack,args)
        self.assertFalse(self.events);self.assertEqual(stack.read(self.pending)['candidate'],repair.FAILED)

    def test_inherited_schema_requires_original_migrator_stop_and_no_new_migrator(self):
        original=self.candidate/'telemetry-transition-state.json'
        stack.write(original,{'phase':'ready_for_workers','migrator':{'name':'telemetry-migrate-'+repair.FAILED,'image':'sha256:original'}})
        state={'kind':'inherited_completed_schema','from_candidate':repair.FAILED,'original_state_sha256':stack.sha(original)}
        with patch.object(stack,'command',return_value=b''):
            self.assertTrue(tm.stop_owned_migrator(stack,self.repaired,state)['stop_proven'])
        with patch.object(stack,'command',return_value=b'unexpected-container'):
            self.assertFalse(tm.stop_owned_migrator(stack,self.repaired,state)['stop_proven'])
        original.write_bytes(original.read_bytes()+b' ')
        self.assertFalse(tm.stop_owned_migrator(stack,self.repaired,state)['stop_proven'])


if __name__=='__main__':unittest.main()
