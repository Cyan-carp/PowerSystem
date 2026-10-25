"""The legacy R0 release gate also fences V4 authorities and local bridges."""
import importlib.util
from pathlib import Path
import unittest
import copy
import datetime
import os
import tempfile
import json
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('production_stack_v4', Path(__file__).with_name('production-stack.py'))
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


class V4ProductionBoundaryTests(unittest.TestCase):
    def test_legacy_and_readonly_candidate_remain_compatible(self):
        ps.validate_v4_readonly({'api': {}, 'agent-rag': {'environment': {
            'V4_ASSISTANT_ENABLED': 'true', 'V4_RUNS_ENABLED': 'true',
            'V4_ARTIFACTS_ENABLED': 'true', 'V4_ACTIONS_ENABLED': 'false',
            'V4_OPS_WRITE_ENABLED': 'false', 'V4_SCHEDULES_ENABLED': 'false'}}})

    def test_new_authorities_cannot_bypass_original_write_switch(self):
        for service in ('api', 'agent-rag', 'ops-agent', 'security-agent'):
            for key in ('V4_ACTIONS_ENABLED', 'V4_OPS_WRITE_ENABLED', 'V4_SCHEDULES_ENABLED',
                        'V4_S06_LOCAL_ENABLED', 'V4_S07_LOCAL_ENABLED', 'V4_S08_LOCAL_ENABLED',
                        'V4_FUTURE_WRITE_ENABLED'):
                for value in ('true', '1', 'yes', '', None):
                    with self.subTest(service=service, key=key, value=value):
                        with self.assertRaisesRegex(ps.Blocked, 'v4_production_profile_not_authorized'):
                            ps.validate_v4_readonly({service: {'environment': {
                                'OPS_WRITE_ENABLED': 'false', key: value}}})

    def test_security_profile_cannot_be_switched_by_v4_release(self):
        ps.validate_v4_readonly({'security-agent': {'environment': {'SEC_RUNTIME_PROFILE': 'm4-server-readonly'}}})
        with self.assertRaises(ps.Blocked):
            ps.validate_v4_readonly({'security-agent': {'environment': {}}})
        with self.assertRaises(ps.Blocked):
            ps.validate_v4_readonly({'security-agent': {'environment': {'SEC_RUNTIME_PROFILE': 'v4-s08-local'}}})

    def test_explicit_old_production_read_bridge_preserves_write_fences(self):
        env = {'V4_READ_DELEGATION_PROTOCOL': 'ma/v1-bound-read',
               'V4_RUNS_ENABLED': 'true', 'V4_ACTIONS_ENABLED': 'false',
               'V4_OPS_WRITE_ENABLED': 'false', 'V4_SCHEDULES_ENABLED': 'false'}
        ps.validate_v4_readonly({'agent-rag': {'environment': env}})
        for key in ('V4_ACTIONS_ENABLED', 'V4_OPS_WRITE_ENABLED', 'V4_SCHEDULES_ENABLED',
                    'V4_S09_LOCAL_ENABLED', 'V4_JOINT_PROFILE'):
            with self.subTest(key=key), self.assertRaises(ps.Blocked):
                ps.validate_v4_readonly({'agent-rag': {'environment': {**env, key: 'true'}}})
        for service in ('api', 'ops-agent', 'security-agent'):
            with self.subTest(service=service), self.assertRaises(ps.Blocked):
                ps.validate_v4_readonly({service: {'environment': env}})
        for value in ('ma-run/v1', '', None, 'false'):
            with self.subTest(protocol=value), self.assertRaises(ps.Blocked):
                ps.validate_v4_readonly({'agent-rag': {'environment': {**env, 'V4_READ_DELEGATION_PROTOCOL': value}}})


class BackupMaintenanceTests(unittest.TestCase):
    def fixture(self, directory):
        base=Path(directory);release=base/'releases/r0';release.mkdir(parents=True)
        names=('agent-rag','ops-agent','api','ops-executor')
        before={name:{'id':name+'-id','image':'sha256:'+('a'*64), 'started_at':'before','restarts':0} for name in names}
        after=copy.deepcopy(before)
        for name in ('agent-rag','ops-agent'):after[name]['started_at']='resumed'
        rows={name:{'Id':s['id'],'Image':s['image'],'RestartCount':s['restarts'],'State':{'StartedAt':s['started_at'],'Running':True,'Health':{'Status':'healthy'}}} for name,s in after.items()}
        pointer={'release':'r0','gate_file':'old.json','manifest_sha256':'frozen','updated_at':'2026-10-25T07:13:00+00:00'}
        manifest={'parent':'compat','services':['api','agent-rag','frontend']}
        ps.write(base/'active.json',pointer);ps.write(release/'old.json',{'states':before})
        archive=base/'powersystem-20261025T000000Z.tar.gz.gpg'
        receipt={'version':1,'active':pointer,'archive':archive.name,'sha256':'digest',
                 'before':before,'after':after,'resumed_at':ps.now()}
        ps.write(Path(str(archive)+'.v4-maintenance.json'),receipt)
        return base,release,archive,pointer,manifest,before,after,rows,receipt

    def test_only_same_writer_restart_times_are_permitted(self):
        with tempfile.TemporaryDirectory() as directory:
            base,release,archive,pointer,manifest,before,after,rows,receipt=self.fixture(directory)
            ps.maintenance_states_match(before,after)
            for name,key in [('agent-rag','id'),('ops-agent','image'),('ops-agent','restarts'),('api','started_at'),('ops-executor','started_at')]:
                changed=copy.deepcopy(after);changed[name][key]='unexpected'
                with self.subTest(name=name,key=key),self.assertRaises(ps.Blocked):
                    ps.maintenance_states_match(before,changed)
            with patch.object(ps,'BACKUP_DIR',base):
                ps.rollback_maintenance_matches(Path(str(archive)+'.v4-maintenance.json'),release,pointer,rows)
                with self.assertRaises(ps.Blocked):
                    ps.rollback_maintenance_matches(Path(str(archive)+'.v4-maintenance.json'),release,{**pointer,'release':'other'},rows)

    def test_requalification_uses_the_full_existing_gate_and_retains_old_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            base,release,archive,pointer,manifest,before,after,rows,receipt=self.fixture(directory)
            with patch.object(ps,'STATE_DIR',base),patch.object(ps,'active',return_value=(release,manifest)),patch.object(ps,'inspect',return_value=rows),patch.object(ps,'verified_full_baseline',return_value={'archive':archive.name,'sha256':'digest'}),patch.object(ps,'gate',return_value='new-gate.json') as gate,patch.object(ps,'set_active') as set_active:
                ps.verify_backup_maintenance(SimpleNamespace(backup=str(archive)))
                gate.assert_called_once_with(release,[],after)
                set_active.assert_called_once_with(release,'new-gate.json',None,'2026-10-25T07:13:00+00:00')
                self.assertFalse((base/'pending.json').exists())
                self.assertTrue((release/'old.json').exists())

    def test_mismatched_receipt_blocks_and_failed_gate_uses_parent_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            base,release,archive,pointer,manifest,before,after,rows,receipt=self.fixture(directory)
            with patch.object(ps,'STATE_DIR',base),patch.object(ps,'active',return_value=(release,manifest)),patch.object(ps,'inspect',return_value=rows),patch.object(ps,'verified_full_baseline',return_value={'archive':archive.name,'sha256':'wrong'}),patch.object(ps,'gate') as gate:
                with self.assertRaisesRegex(ps.Blocked,'receipt_mismatch'):
                    ps.verify_backup_maintenance(SimpleNamespace(backup=str(archive)))
                gate.assert_not_called();self.assertFalse((base/'pending.json').exists())
            with patch.object(ps,'STATE_DIR',base),patch.object(ps,'active',return_value=(release,manifest)),patch.object(ps,'inspect',return_value=rows),patch.object(ps,'verified_full_baseline',return_value={'archive':archive.name,'sha256':'digest'}),patch.object(ps,'gate',side_effect=ps.Blocked('gate_failed')),patch.object(ps,'restore_transaction') as rollback:
                with self.assertRaisesRegex(ps.Blocked,'gate_failed'):
                    ps.verify_backup_maintenance(SimpleNamespace(backup=str(archive)))
                transaction=rollback.call_args.args[0]
                self.assertEqual(transaction['previous'],'compat')
                self.assertEqual(transaction['before'],after)


class PublishBaselineTests(unittest.TestCase):
    """A full backup pauses the two writers, so publish must read its own receipt."""

    def fixture(self, directory):
        base=Path(directory);release=base/'releases/r0';release.mkdir(parents=True)
        names=('agent-rag','ops-agent','api','ops-executor')
        frozen={n:{'id':n+'-id','image':'sha256:'+('a'*64),'started_at':'frozen','restarts':0} for n in names}
        running=copy.deepcopy(frozen)
        for n in ('agent-rag','ops-agent'):running[n]['started_at']='resumed'
        parent={'release':'r0','manifest_version':1}
        pointer={'release':'r0','gate_file':'old.json','manifest_sha256':ps.digest(parent),
                 'updated_at':'2026-10-25T07:13:00+00:00'}
        archive=base/'powersystem-20261025T101427Z.tar.gz.gpg';archive.write_bytes(b'ciphertext')
        ps.write(release/'old.json',{'passed':True,'states':frozen})
        receipt={'version':1,'active':pointer,'archive':archive.name,'sha256':'digest',
                 'before':frozen,'after':running,
                 'started_at':'2026-10-25T10:14:30+00:00','resumed_at':'2026-10-25T10:17:54+00:00'}
        ps.write(Path(str(archive)+'.v4-maintenance.json'),receipt)
        manifest={'parent':'r0','created_at':'2026-10-25T10:13:59+00:00','before':frozen}
        return base,release,archive,pointer,parent,manifest,frozen,running,receipt

    def rows(self, values):
        return {name:{'Id':state['id'],'Image':state['image'],'RestartCount':state['restarts'],
                      'State':{'StartedAt':state['started_at'],'Running':True,
                               'Health':{'Status':'healthy'}}}
                for name,state in values.items()}

    def test_candidate_own_backup_receipt_explains_both_baselines(self):
        with tempfile.TemporaryDirectory() as directory:
            base,release,archive,pointer,parent,manifest,frozen,running,receipt=self.fixture(directory)
            with patch.multiple(ps,STATE_DIR=base,BACKUP_DIR=base,
                                verified_full_baseline=lambda _archive:{'archive':archive.name,'sha256':'digest'}):
                value=ps.maintenance_receipt(archive,manifest,parent,'digest')
                self.assertEqual(value['after'],running)
                self.assertEqual(ps.require_baseline(running,frozen,value,'candidate_baseline_drift'),'backup_maintenance')
                self.assertEqual(ps.require_baseline(running,running,value,'candidate_baseline_drift'),'snapshot')

    def test_receipt_cannot_excuse_drift_outside_the_two_writers(self):
        with tempfile.TemporaryDirectory() as directory:
            base,release,archive,pointer,parent,manifest,frozen,running,receipt=self.fixture(directory)
            with patch.multiple(ps,STATE_DIR=base,BACKUP_DIR=base,
                                verified_full_baseline=lambda _archive:{'archive':archive.name,'sha256':'digest'}):
                value=ps.maintenance_receipt(archive,manifest,parent,'digest')
                unexpected=copy.deepcopy(running);unexpected['api']['started_at']='restarted-by-someone'
                with self.assertRaisesRegex(ps.Blocked,'candidate_baseline_drift'):
                    ps.require_baseline(unexpected,frozen,value,'candidate_baseline_drift')
                with self.assertRaisesRegex(ps.Blocked,'running_baseline_drift'):
                    ps.require_baseline(unexpected,frozen,value,'running_baseline_drift')

    def test_receipt_must_bind_parent_archive_and_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            base,release,archive,pointer,parent,manifest,frozen,running,receipt=self.fixture(directory)
            cases=[('parent',{'active':{**pointer,'release':'other'}},'backup_maintenance_parent_mismatch'),
                   ('gate',{'active':{**pointer,'gate_file':'other-gate.json'}},'backup_maintenance_baseline_mismatch'),
                   ('archive',{'sha256':'other'},'backup_maintenance_receipt_archive_mismatch'),
                   ('extension',{'version':2},'backup_maintenance_receipt_incomplete'),
                   ('resume',{'resumed_at':'2026-10-25T10:00:00+00:00'},'backup_maintenance_receipt_incomplete')]
            ps.write(release/'other-gate.json',{'states':{**frozen,'api':{**frozen['api'],'started_at':'other'}}})
            with patch.multiple(ps,STATE_DIR=base,BACKUP_DIR=base,
                                verified_full_baseline=lambda _archive:{'archive':archive.name,'sha256':'digest'}):
                for name,change,reason in cases:
                    with self.subTest(case=name),self.assertRaisesRegex(ps.Blocked,reason):
                        ps.write(Path(str(archive)+'.v4-maintenance.json'),{**receipt,**change})
                        ps.maintenance_receipt(archive,manifest,parent,'digest')
                ps.write(Path(str(archive)+'.v4-maintenance.json'),receipt)
                other=copy.deepcopy(frozen);other['api']['started_at']='other'
                with self.subTest(case='candidate'),self.assertRaisesRegex(ps.Blocked,'backup_maintenance_candidate_mismatch'):
                    ps.maintenance_receipt(archive,{**manifest,'before':other},parent,'digest')
                with self.assertRaisesRegex(ps.Blocked,'backup_maintenance_receipt_predates_candidate'):
                    ps.maintenance_receipt(archive,{**manifest,'created_at':'2026-10-25T10:15:00+00:00'},parent,'digest')
                with self.assertRaisesRegex(ps.Blocked,'backup_maintenance_archive_changed'):
                    ps.maintenance_receipt(archive,manifest,parent,'other')

    def test_absent_or_stale_receipt_keeps_the_original_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            base,release,archive,pointer,parent,manifest,frozen,running,receipt=self.fixture(directory)
            Path(str(archive)+'.v4-maintenance.json').unlink()
            with patch.multiple(ps,STATE_DIR=base,BACKUP_DIR=base,
                                verified_full_baseline=lambda _archive:{'archive':archive.name,'sha256':'digest'}):
                self.assertIsNone(ps.maintenance_receipt(archive,manifest,parent,'digest'))
                with self.assertRaisesRegex(ps.Blocked,'candidate_baseline_drift'):
                    ps.require_baseline(running,frozen,None,'candidate_baseline_drift')

    def test_fast_baseline_boundary_uses_activation_not_requalification(self):
        with tempfile.TemporaryDirectory() as directory:
            base,release,archive,pointer,parent,manifest,frozen,running,receipt=self.fixture(directory)
            snapshot=datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=1)
            os.utime(archive,(snapshot.timestamp(),snapshot.timestamp()))
            baseline={'archive':archive.name,'sha256':'digest',
                      'snapshot_started_at':snapshot.isoformat(),'created_at':snapshot.isoformat()}
            activated=snapshot-datetime.timedelta(hours=1)
            resigned=snapshot+datetime.timedelta(minutes=20)
            with patch.multiple(ps,STATE_DIR=base,BACKUP_DIR=base,
                                verified_full_baseline=lambda _archive:baseline):
                self.assertEqual(ps.choose_fast_baseline(
                    {**pointer,'published_at':activated.isoformat(),'updated_at':resigned.isoformat()})['archive'],
                    archive.name)
                ps.write(release/('backup-maintenance-1.json'),
                         {'previous_pointer':{**pointer,'updated_at':activated.isoformat()},
                          'gate_file':'new-gate.json'})
                self.assertEqual(ps.choose_fast_baseline(
                    {**pointer,'gate_file':'new-gate.json','updated_at':resigned.isoformat()})['archive'],
                    archive.name)
                with self.assertRaisesRegex(ps.Blocked,'fast_baseline_must_follow_current_release'):
                    ps.choose_fast_baseline({**pointer,'gate_file':'other-gate.json',
                                             'updated_at':resigned.isoformat()})

    def test_requalification_keeps_the_activation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            base,release,archive,pointer,parent,manifest,frozen,running,receipt=self.fixture(directory)
            ps.write(release/'manifest.json',{'release':'r0'})
            ps.write(base/'active.json',pointer)
            rows=self.rows(running)
            with patch.multiple(ps,STATE_DIR=base,BACKUP_DIR=base,
                                active=lambda:(release,{'parent':'compat','services':['frontend']}),
                                inspect=lambda:rows,
                                verified_full_baseline=lambda _archive:{'archive':archive.name,'sha256':'digest'},
                                gate=lambda *args:'new-gate.json'):
                ps.verify_backup_maintenance(SimpleNamespace(backup=str(archive)))
            value=ps.read(base/'active.json')
            self.assertEqual(value['gate_file'],'new-gate.json')
            self.assertEqual(value['published_at'],pointer['updated_at'])
            self.assertNotEqual(value['updated_at'],pointer['updated_at'])


class FrontendAssetTests(unittest.TestCase):
    """A stale build context silently drops route chunks; prepare must refuse it."""

    def test_only_the_build_hash_is_ignored(self):
        self.assertEqual(
            ps.chunk_names(['AgentSchedulesView-BYDNIkqa.js', 'index-AbCdEf12.js',
                            'LoginView-By5KEgWA.js', 'installCanvasRenderer-EuOyc_8b.js',
                            'AgentChatView-BYDNIkqa.css', 'logo-abc123.png']),
            {'AgentSchedulesView', 'index', 'LoginView', 'installCanvasRenderer',
             'AgentChatView'})

    def test_only_chunks_the_candidate_lost_are_reported(self):
        with patch.object(ps, 'frontend_chunk_names',
                          side_effect=[{'index', 'AgentChatView', 'AgentSchedulesView'},
                                       {'index', 'AgentChatView'}]):
            self.assertEqual(ps.missing_chunks('candidate', 'baseline'), ['AgentSchedulesView'])
        with patch.object(ps, 'frontend_chunk_names',
                          side_effect=[{'index'}, {'index', 'AgentSchedulesView'}]):
            self.assertEqual(ps.missing_chunks('candidate', 'baseline'), [])


if __name__ == '__main__':
    unittest.main()
