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


class ExplicitProductionProfileTests(unittest.TestCase):
    def fixture(self, base):
        root=Path(base);identity=root/'runtime/batch/private/production';identity.mkdir(parents=True)
        raw=b'synthetic-owned-machine\n';machine=ps.hashlib.sha256(raw).hexdigest()
        host={'hostname':'powersystem-prod','machine_id_sha256':machine,'dmi_uuid':'synthetic-dmi'}
        ps.write(identity/'identity.json',{'profile':'v4-business-schedule/v1',
            'host':{**host,'vm_id':117},
            'databases':{'business':'powersystem','rag':'powersystem_agent_rag','ops':'ma_ops'},
            'origins':{'api':'http://api:8080','rag':'http://agent-rag:8093','ops':'http://ops-agent:8094','security':'http://security-agent:8100'},
            'capabilities':{'actions':True,'schedules':True,'ops_write':False},
            'policies':{'business':'v4-s06-production/v1','schedule':'v4-s08-production/v1'}})
        (identity/'machine-id').write_bytes(raw);(identity/'product-uuid').write_text('synthetic-dmi')
        common={'V4_PRODUCTION_PROFILE':'v4-business-schedule/v1'}
        action={**common,'V4_ACTIONS_ENABLED':'true','V4_SCHEDULES_ENABLED':'true','V4_OPS_WRITE_ENABLED':'false'}
        services={n:{'environment':copy.deepcopy(e),'volumes':[{'type':'bind','source':str(identity),'target':'/run/v4-production','read_only':True}]} for n,e in
                  {'api':action,'ops-agent':action,'security-agent':common,
                   'agent-rag':{**action,'V4_ASSISTANT_ENABLED':'true','V4_RUNS_ENABLED':'true','V4_ARTIFACTS_ENABLED':'true','V4_READ_DELEGATION_PROTOCOL':'ma-run/v1'}}.items()}
        services['ops-agent']['environment'].update(OPS_RUNTIME_PROFILE='m4-server-readonly',OPS_WRITE_ENABLED='false',OPS_INFRA_ENABLED='true',OPS_SECURITY_ENABLED='true',OPS_INSPECTION_ENABLED='true')
        services['security-agent']['environment'].update(SEC_RUNTIME_PROFILE='m4-server-readonly',SEC_ACTION_LLM_URL='https://api.deepseek.com',SEC_ACTION_MODEL='deepseek-flash',SEC_ACTION_LLM_API_KEY_FILE='/run/action/key',SEC_LLM_API_KEY_FILE='/run/old/key')
        services['agent-rag']['environment'].update(RAG_TOOL_TIMEOUT='45',SCHEDULE_BRIDGE_TOKEN_FILE='/run/bridge/key')
        services['api']['environment']['SCHEDULE_BRIDGE_TOKEN_FILE']='/run/bridge/key'
        return root,services,host,identity

    def test_explicit_profile_retains_machine_and_legacy_domain_fences(self):
        with tempfile.TemporaryDirectory() as directory:
            root,services,host,identity=self.fixture(directory)
            with patch.object(ps,'ROOT',root),patch.object(ps,'registered_business_host',return_value=host):
                ps.validate_v4_readonly(services)
                mutations=[('ops-agent','V4_OPS_WRITE_ENABLED','true'),('agent-rag','V4_READ_DELEGATION_PROTOCOL','ma/v1-bound-read'),
                           ('api','V4_JOINT_PROFILE','v4-s09-local'),('security-agent','SEC_ACTION_LLM_URL','https://other.example'),
                           ('agent-rag','RAG_TOOL_TIMEOUT','15'),('ops-agent','OPS_RUNTIME_PROFILE','legacy')]
                for service,key,value in mutations:
                    changed=copy.deepcopy(services);changed[service]['environment'][key]=value
                    with self.subTest(service=service,key=key),self.assertRaises(ps.Blocked):ps.validate_v4_readonly(changed)
                (identity/'machine-id').write_bytes(b'another-machine')
                with self.assertRaises(ps.Blocked):ps.validate_v4_readonly(services)

    def test_partial_profile_and_writeable_identity_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root,services,host,_=self.fixture(directory)
            with patch.object(ps,'ROOT',root),patch.object(ps,'registered_business_host',return_value=host):
                changed=copy.deepcopy(services);changed['api']['environment'].pop('V4_PRODUCTION_PROFILE')
                with self.assertRaises(ps.Blocked):ps.validate_v4_readonly(changed)
                changed=copy.deepcopy(services);changed['ops-agent']['volumes'][0]['read_only']=False
                with self.assertRaises(ps.Blocked):ps.validate_v4_readonly(changed)

    def test_assistant_runs_are_drained_before_protocol_transition(self):
        with patch.object(ps,'command',side_effect=[b'0',b't',b'1']):
            with self.assertRaisesRegex(ps.Blocked,'inflight_assistant_runs_block_release'):ps.drain()
        with patch.object(ps,'command',side_effect=[b'0',b't',b'0']):ps.drain()


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


class BackupDeferralTests(unittest.TestCase):
    """Two named candidates may publish once without a fresh full backup, but only
    through the owner authorization frozen into the candidate itself."""

    candidate = 'v4-business-schedule-20261026-r1'
    parent = 'ui-approval-20261026-r1'

    def authorization(self, **change):
        value = {'schema': 1, 'kind': 'backup_deferral_authorization',
                 'candidate': self.candidate, 'parent': self.parent,
                 'services': list(ps.DEFERRAL_SERVICES),
                 'owner_requested_defer_until_effects_accepted': True,
                 'owner_instruction': '激进一点，连做大阶段后验证，本次不要全量备份，效果先呈现',
                 'recorded_at': '2026-10-26T00:00:00Z'}
        value.update(change)
        return value

    def fixture(self, directory, **change):
        base = Path(directory)
        previous = base / 'releases' / self.parent
        candidate = base / 'releases' / self.candidate
        previous.mkdir(parents=True)
        candidate.mkdir(parents=True)
        names = ('api', 'agent-rag', 'ops-agent', 'security-agent')
        frozen = {n: {'id': n + '-id', 'image': 'sha256:' + ('a' * 64),
                      'started_at': 'frozen', 'restarts': 0} for n in names}
        rows = {n: {'Id': s['id'], 'Image': s['image'], 'RestartCount': 0,
                    'State': {'StartedAt': s['started_at'], 'Running': True,
                              'Health': {'Status': 'healthy'}}}
                for n, s in frozen.items()}
        ps.write(base / 'active.json', {'release': self.parent, 'gate_file': 'old.json',
                                        'manifest_sha256': 'parent-digest',
                                        'updated_at': '2026-10-26T00:00:00+00:00'})
        ps.write(previous / 'old.json', {'passed': True, 'states': frozen})
        ps.write(previous / 'acceptance-pointer.json', {'release': self.parent})
        ps.write(candidate / 'private/compose.json', {'name': ps.PROJECT})
        doc = self.authorization(**change)
        source = base / 'owner-authorization.json'
        ps.write(source, doc)
        block = ps.bind_backup_deferral(candidate, self.candidate, self.parent, source, doc)
        m = {'manifest_version': 1, 'release': self.candidate, 'parent': self.parent,
             'created_at': '2026-10-26T00:00:00+00:00', 'backup_tier': ps.FULL_TIER,
             'services': list(ps.DEFERRAL_SERVICES),
             'files': {ps.DEFERRAL_FILE: block['sha256']}, 'images': {},
             'private_references': {},
             'origin_environment': str(base / 'env'), 'before': frozen,
             'backup_deferral': block}
        return base, previous, candidate, frozen, rows, m

    def publish(self, base, previous, candidate, rows, m, backup=None):
        captured = {}

        def activate(path, gate_name, baseline=None):
            captured['activation'] = (path, gate_name, baseline)
            captured['pending'] = ps.read(base / 'pending.json')

        with patch.multiple(ps, ROOT=base, STATE_DIR=base, BACKUP_DIR=base,
                            active=lambda: (previous, {'parent': None}),
                            immutable=lambda path: m, inspect=lambda: rows,
                            drain=lambda: None, deploy=lambda *args: None,
                            gate=lambda *args: 'accepted.json',
                            preflight=lambda *args: None, healthy=lambda *args: None,
                            set_active=activate):
            ps.publish(SimpleNamespace(id=self.candidate, backup=backup))
        return captured

    def test_frozen_authorization_allows_one_publish_without_a_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            base, previous, candidate, frozen, rows, m = self.fixture(directory)
            # The bind result alone must carry the owner's boolean; nothing is patched in.
            self.assertIs(m['backup_deferral']['owner_requested_defer_until_effects_accepted'],
                          True)
            captured = self.publish(base, previous, candidate, rows, m)
            self.assertFalse((base / 'pending.json').exists())
            self.assertEqual(captured['activation'][2], None)
            pending = captured['pending']
            self.assertNotIn('predeployment_backup', pending)
            self.assertNotIn('backup_sha256', pending)
            self.assertEqual(pending['backup_deferred']['effects_accepted'], False)
            self.assertEqual(pending['backup_deferred']['authorization_sha256'],
                             ps.sha(candidate / ps.DEFERRAL_FILE))
            self.assertIn('不要全量备份', pending['backup_deferred']['owner_instruction'])
            self.assertEqual(ps.sha(candidate / ps.DEFERRAL_FILE),
                             ps.sha(base / 'owner-authorization.json'))

    def test_no_authorization_keeps_the_backup_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            base, previous, candidate, frozen, rows, m = self.fixture(directory)
            without = {k: v for k, v in m.items() if k != 'backup_deferral'}
            with self.assertRaisesRegex(ps.Blocked, 'predeployment_backup_required'):
                self.publish(base, previous, candidate, rows, without)
            self.assertFalse((base / 'pending.json').exists())

    def test_a_regular_backup_argument_is_still_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            base, previous, candidate, frozen, rows, m = self.fixture(directory)
            wrong = base / 'not-a-production-archive.tar.gz.gpg'
            wrong.write_bytes(b'x')
            with self.assertRaisesRegex(ps.Blocked, 'owned_encrypted_backup_required'):
                self.publish(base, previous, candidate, rows, m, str(wrong))
            self.assertFalse((base / 'pending.json').exists())

    def test_frozen_copy_and_manifest_binding_cannot_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            base, previous, candidate, frozen, rows, m = self.fixture(directory)
            with patch.object(ps, 'ROOT', base):
                ps.write(candidate / 'manifest.json', m)
                ps.immutable(candidate)
                frozen_path = candidate / ps.DEFERRAL_FILE
                original = frozen_path.read_bytes()
                frozen_path.write_text(original.decode() + ' ')
                with self.assertRaisesRegex(ps.Blocked, 'frozen_artifact_changed'):
                    ps.immutable(candidate)
                with self.assertRaisesRegex(ps.Blocked, 'backup_deferral_frozen_file_changed'):
                    ps.frozen_backup_deferral(candidate, m, self.parent)
                frozen_path.write_bytes(original)
                ps.immutable(candidate)
                unbound = copy.deepcopy(m)
                unbound['backup_deferral']['sha256'] = 'other'
                ps.write(candidate / 'manifest.json', unbound)
                with self.assertRaisesRegex(ps.Blocked, 'frozen_backup_deferral_unbound'):
                    ps.immutable(candidate)

    def test_authorization_fields_are_explicit_and_candidate_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cases = [
                ('allowlist', 'another-release-20261026-r1',
                 {'candidate': 'another-release-20261026-r1', 'parent': self.parent},
                 ps.FULL_TIER, list(ps.DEFERRAL_SERVICES),
                 'backup_deferral_not_authorized_for_candidate'),
                ('candidate', self.candidate,
                 {'candidate': 'v4-production-compat-20261026-r2'},
                 ps.FULL_TIER, list(ps.DEFERRAL_SERVICES),
                 'backup_deferral_candidate_or_parent_mismatch'),
                ('parent', self.candidate, {'parent': 'other-parent-20261026-r1'},
                 ps.FULL_TIER, list(ps.DEFERRAL_SERVICES),
                 'backup_deferral_candidate_or_parent_mismatch'),
                ('flag', self.candidate,
                 {'owner_requested_defer_until_effects_accepted': 'true'},
                 ps.FULL_TIER, list(ps.DEFERRAL_SERVICES),
                 'backup_deferral_owner_request_required'),
                ('scope', self.candidate,
                 {'services': ['api', 'frontend', 'ops-agent', 'security-agent']},
                 ps.FULL_TIER, list(ps.DEFERRAL_SERVICES), 'backup_deferral_scope_mismatch'),
                ('instruction', self.candidate, {'owner_instruction': 'defer'},
                 ps.FULL_TIER, list(ps.DEFERRAL_SERVICES),
                 'backup_deferral_owner_instruction_required'),
                ('timestamp', self.candidate, {'recorded_at': '2999-01-01T00:00:00Z'},
                 ps.FULL_TIER, list(ps.DEFERRAL_SERVICES),
                 'backup_deferral_timestamp_in_future'),
                ('offset', self.candidate, {'recorded_at': '2026-10-26T08:00:00+08:00'},
                 ps.FULL_TIER, list(ps.DEFERRAL_SERVICES),
                 'backup_deferral_timestamp_invalid'),
                ('fast-tier', self.candidate, {}, ps.FAST_TIER,
                 list(ps.DEFERRAL_SERVICES), 'backup_deferral_not_authorized_for_candidate'),
                ('release-scope', self.candidate, {}, ps.FULL_TIER,
                 ['api', 'agent-rag', 'ops-agent', 'security-agent', 'frontend'],
                 'backup_deferral_release_scope_mismatch'),
            ]
            with patch.object(ps, 'ROOT', base):
                for name, candidate_id, change, tier, services, reason in cases:
                    source = base / (name + '.json')
                    ps.write(source, self.authorization(**change))
                    with self.subTest(case=name), self.assertRaisesRegex(ps.Blocked, reason):
                        ps.read_backup_deferral(source, candidate_id, self.parent,
                                                tier, services)
                source = base / 'compat.json'
                ps.write(source, self.authorization(candidate='v4-production-compat-20261026-r1'))
                ps.read_backup_deferral(source, 'v4-production-compat-20261026-r1',
                                        self.parent, ps.FULL_TIER,
                                        list(ps.DEFERRAL_SERVICES))


if __name__ == '__main__':
    unittest.main()
