"""The four live readonly /run mounts, including Security's detached action key."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

def module(name, filename):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(filename))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value

backup=module('backup_ma_release_config','backup-ma.py')

class ReleaseConfigurationTests(unittest.TestCase):
    def setUp(self):
        self._tmp=tempfile.TemporaryDirectory();self.root=Path(self._tmp.name).resolve();self.ma=self.root/'archive/ma';self.ma.mkdir(parents=True)
        self.private=self.root/'runtime/ma-m4-vm117/private'
        self.identity=self.tree('identity',{'identity.json':b'{}','machine-id':b'synthetic-machine-id'})
        self.bridge=self.tree('v4-production-bridge',{'token':b'synthetic-bridge-token'})
        self.api=self.tree('api',{'actor-key':b'synthetic-api-actor'})
        self.rag=self.tree('supervisor',{'actor-key':b'synthetic-rag-actor'})
        self.ops=self.tree('ops',{'service-token':b'synthetic-ops-token'})
        self.security=self.tree('security',{'old-key':b'synthetic-old-security-key'})
        self.action=self.private/'v4-production-action-key';self.action.write_bytes(b'synthetic-live-action-key')
        self.fleet=self.private/'fleet.json';self.fleet.write_bytes(b'{"devices":["fixture"]}')

    def tearDown(self):
        self._tmp.cleanup()

    def tree(self,name,files):
        base=self.private/name
        base.mkdir(parents=True,exist_ok=True)
        for key,value in files.items():(base/key).write_bytes(value)
        return base

    def rows(self):
        def row(role,mounts,service=None,project='powersystem-stage4'):
            return {'Config':{'Labels':{'com.docker.compose.project':project,'com.docker.compose.service':service or role}},'Mounts':mounts}
        def mount(source,target,rw=False,kind='bind'):
            return {'Source':str(source),'Destination':target,'Type':kind,'RW':rw}
        identity=mount(self.identity,'/run/v4-production')
        return {
            'api':row('api',[identity,mount(self.bridge,'/run/v4-schedule-bridge'),mount(self.api,'/run/ma-api'),mount(self.fleet,'/run/powersystem-fleet/fleet.json')]),
            'agent-rag':row('agent-rag',[identity,mount(self.bridge,'/run/v4-schedule-bridge'),mount(self.rag,'/run/ma-supervisor')]),
            'ops-agent':row('ops-agent',[identity,mount(self.ops,'/run/ma'),mount(self.private.parent/'audit','/audit',rw=True)]),
            'security-agent':row('security-agent',[identity,mount(self.security,'/run/ma-security'),mount(self.action,'/run/v4-security-action-key')])}

    def capture(self,rows=None):
        with patch.object(backup,'ROOT',self.root):return backup.capture_release_configuration(self.ma,rows or self.rows())

    def test_new_security_key_and_api_fleet_are_captured(self):
        value=self.capture()
        files=value['files']
        action=next(item for item in files.values() if item['source']==str(self.action))
        fleet=next(item for item in files.values() if item['source']==str(self.fleet))
        self.assertEqual((self.ma/action['archive']).read_bytes(),b'synthetic-live-action-key')
        self.assertEqual((self.ma/fleet['archive']).read_bytes(),b'{"devices":["fixture"]}')
        self.assertIn('/run/v4-security-action-key',value['services']['security-agent']['target'])
        self.assertIn('/run/powersystem-fleet/fleet.json',value['services']['api']['target'])
        self.assertTrue(value['restore_execution_forbidden'])
        self.assertIn('mode',action)
        self.assertTrue(action['archive'].startswith('release-runtime/runtime/ma-m4-vm117/private/'))

    def test_metadata_never_contains_credential_values(self):
        text=json.dumps(self.capture())
        for secret in ('synthetic-live-action-key','synthetic-bridge-token','synthetic-ops-token'):
            self.assertNotIn(secret,text)

    def test_shared_identity_is_deduplicated_across_services(self):
        value=self.capture()
        machine=next(item for item in value['files'].values() if item['source']==str(self.identity/'machine-id'))
        self.assertEqual(sorted(machine['services']),sorted(backup.RELEASE_SERVICES))
        self.assertEqual(sum(1 for name in value['files'] if name.endswith('/machine-id')),1)
        self.assertEqual(value['counts']['files'],len(value['files']))

    def test_non_run_rw_mount_is_ignored(self):
        value=self.capture()
        self.assertNotIn(str(self.private.parent/'audit'),json.dumps(value))

    def test_rw_run_mount_is_rejected(self):
        rows=self.rows();rows['ops-agent']['Mounts'][1]['RW']=True
        with self.assertRaisesRegex(ValueError,'readonly_release_private_bind'):
            self.capture(rows)

    def test_source_outside_runtime_is_rejected(self):
        outside=self.root/'outside';outside.mkdir();(outside/'key').write_bytes(b'host-file')
        rows=self.rows();rows['security-agent']['Mounts'][2]['Source']=str(outside/'key')
        with self.assertRaisesRegex(ValueError,'actual_release_private_source'):
            self.capture(rows)

    def test_symlinked_source_is_rejected(self):
        link=self.private/'linked-ops'
        try:link.symlink_to(self.ops,target_is_directory=True)
        except OSError:
            link.mkdir();real=Path.is_symlink
            with patch.object(Path,'is_symlink',lambda p:True if Path(p)==link else real(p)):
                rows=self.rows();rows['ops-agent']['Mounts'][1]['Source']=str(link)
                with self.assertRaisesRegex(ValueError,'actual_release_private_source'):
                    self.capture(rows)
            return
        rows=self.rows();rows['ops-agent']['Mounts'][1]['Source']=str(link)
        with self.assertRaisesRegex(ValueError,'actual_release_private_source'):
            self.capture(rows)

    def test_symlink_inside_tree_is_rejected(self):
        nested=self.api/'nested'
        try:nested.symlink_to(self.fleet)
        except OSError:
            nested.write_bytes(b'fixture');real=Path.is_symlink
            with patch.object(Path,'is_symlink',lambda p:True if Path(p)==nested else real(p)):
                with self.assertRaisesRegex(ValueError,'release_private_symlink_forbidden'):
                    self.capture()
            return
        with self.assertRaisesRegex(ValueError,'release_private_symlink_forbidden'):
            self.capture()

    def test_wrong_project_or_role_is_rejected(self):
        rows=self.rows();rows['api']['Config']['Labels']['com.docker.compose.project']='other'
        with self.assertRaisesRegex(ValueError,'owned_release_service'):
            self.capture(rows)
        rows=self.rows();rows['api']['Config']['Labels']['com.docker.compose.service']='agent-rag'
        with self.assertRaisesRegex(ValueError,'owned_release_service'):
            self.capture(rows)

    def test_missing_required_mounts_are_rejected(self):
        rows=self.rows();rows['api']['Mounts']=[m for m in rows['api']['Mounts'] if not m['Destination'].startswith('/run/powersystem-fleet')]
        with self.assertRaisesRegex(ValueError,'required_release_mount_missing:api'):
            self.capture(rows)
        rows=self.rows();rows['security-agent']['Mounts']=[m for m in rows['security-agent']['Mounts'] if m['Destination']!='/run/v4-security-action-key']
        with self.assertRaisesRegex(ValueError,'required_release_mount_missing:security-agent'):
            self.capture(rows)

if __name__=='__main__':unittest.main()
