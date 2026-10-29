"""Compatible release admission, artifact recovery and production smoke scopes.

All state is synthetic. Docker, HTTPS, SSH and GPG are replaced at the boundary.
"""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
import urllib.error
import contextlib

HERE = Path(__file__).resolve().parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


ps = module('compatible_stack', HERE/'production-stack.py')
capsule = module('compatible_capsule', HERE/'frontend-release-backup.py')
sources = module('compatible_sources', HERE/'source-build.py')
edge = module('compatible_edge', HERE.parents[2]/'deploy/edge-server/独立云服务器部署/scripts/check-auth-wss.py')


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(ps, 'ROOT', self.root)
        self.state_patch = patch.object(ps, 'STATE_DIR', self.root/'runtime/state')
        self.root_patch.start()
        self.state_patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.state_patch.stop)
        self.old_image, self.new_image = 'sha256:'+'a'*64, 'sha256:'+'b'*64

    def assessment(self):
        evidence = self.root/'runtime/batch/tests.log'
        evidence.parent.mkdir(parents=True)
        evidence.write_text('real entry targeted test: passed', encoding='utf-8')
        document = {'format': 1, 'kind': 'compatible-release/v1',
                    'candidate': 'patch-r1', 'parent': 'parent-r1',
                    'tested_images': {'api': self.new_image}, 'rpo_seconds': 86400,
                    'rollback_compatible': True, 'production_config_matched': True,
                    **{key: False for key in ('schema_changed', 'data_repair', 'persistent_semantics_changed',
                        'permissions_changed', 'storage_changed', 'contracts_changed')},
                    'tests': [{'name': 'targeted-api', 'exit_code': 0,
                               'evidence': str(evidence), 'sha256': ps.sha(evidence)}]}
        path = self.root/'runtime/batch/assessment.json'
        ps.write(path, document)
        return path, document, evidence

    def test_assessment_binds_tested_images_and_real_evidence(self):
        path, document, evidence = self.assessment()
        self.assertEqual(ps.standard_assessment(path, 'patch-r1', 'parent-r1',
                         {'api': self.new_image})[1], [evidence])
        evidence.write_text('replaced result', encoding='utf-8')
        with self.assertRaisesRegex(ps.Blocked, 'evidence_changed'):
            ps.standard_assessment(path, 'patch-r1', 'parent-r1', {'api': self.new_image})

    def test_high_risk_or_missing_declarations_and_failed_tests_are_rejected(self):
        path, document, _ = self.assessment()
        for key, value in [('schema_changed', True), ('persistent_semantics_changed', True), ('permissions_changed', True),
                           ('contracts_changed', True), ('data_repair', True),
                           ('storage_changed', True), ('rollback_compatible', False),
                           ('production_config_matched', False), ('rpo_seconds', 172800),
                           ('tested_images', {'api': self.old_image}), ('tests', [])]:
            changed = {**document, key: value}
            ps.write(path, changed)
            with self.subTest(key=key), self.assertRaises(ps.Blocked):
                ps.standard_assessment(path, 'patch-r1', 'parent-r1', {'api': self.new_image})
        document['tests'][0]['exit_code'] = 1
        ps.write(path, document)
        with self.assertRaisesRegex(ps.Blocked, 'test_failed'):
            ps.standard_assessment(path, 'patch-r1', 'parent-r1', {'api': self.new_image})

    def config(self):
        old = {'services': {'api': {'image': self.old_image, 'environment': {'WRITE': 'false'}},
                            'frontend': {'image': self.old_image}}, 'volumes': {'data': {'name': 'data'}}}
        previous = ps.release('parent-r1')
        ps.write(previous/'private/compose.json', old)
        ps.write(previous/'private/origin-args.json', ['compose'])
        ps.write(previous/'manifest.json', {'origin_environment': '/private/env'})
        files = {'stages/02-backend/deploy/postgres/001.sql': {'sha256': 'schema', 'mode': 420},
                 'stages/02-backend/internal/api/server.go': {'sha256': 'old-code', 'mode': 420}}
        old_source = {'format': 1, 'files': files}
        new_source = copy.deepcopy(old_source)
        new_source['files']['stages/02-backend/internal/api/server.go']['sha256'] = 'patched-code'
        return old, old_source, new_source

    def source_records(self, original, current):
        store = self.root/'runtime/source-builds'
        before, after = ps.digest(original), ps.digest(current)
        ps.write(store/'contexts'/before/'source-manifest.json', original)
        ps.write(store/'contexts'/after/'source-manifest.json', current)
        ps.write(store/'receipts'/(self.old_image[7:]+'.json'), {
            'image': self.old_image, 'service': 'api', 'source_sha256': before})
        return [{'service': 'api', 'source_sha256': after}]

    def test_api_patch_is_admitted_but_migration_and_config_drift_are_rejected(self):
        old, original, current = self.config()
        records = self.source_records(original, current)
        config = copy.deepcopy(old)
        config['services']['api']['image'] = self.new_image
        args = (['compose'], '/private/env', 'parent-r1', {'api'}, {'api': 'tag'})
        metadata = [{'Id': self.old_image, 'Config': {'Labels': {
            'io.powersystem.source.sha256': ps.digest(original), 'io.powersystem.source.service': 'api'}}}]
        with patch.object(ps, 'command', return_value=json.dumps(metadata).encode()):
            ps.validate_standard_config(config, *args, records)
            changed = copy.deepcopy(config)
            changed['services']['api']['environment']['WRITE'] = 'true'
            with self.assertRaisesRegex(ps.Blocked, 'configuration_changed'):
                ps.validate_standard_config(changed, *args, records)
            current['files']['stages/02-backend/deploy/postgres/001.sql']['sha256'] = 'new-schema'
            records = self.source_records(original, current)
            with self.assertRaisesRegex(ps.Blocked, 'protected_source_changed'):
                ps.validate_standard_config(config, *args, records)
            metadata[0]['Config']['Labels']['io.powersystem.source.sha256'] = 'unrelated'
        with patch.object(ps, 'command', return_value=json.dumps(metadata).encode()):
            with self.assertRaisesRegex(ps.Blocked, 'parent_image_source_mismatch'):
                ps.validate_standard_config(config, *args, records)

    def test_protected_sources_include_contracts_dependencies_and_deletions(self):
        files = {name: {'sha256': 'fixed'} for name in (
            'stages/08-agent-rag/rag_agent/schema.py',
            'stages/09-ops-agent/schedule_contract.py',
            'stages/09-ops-agent/requirements.txt',
            'stages/10-security-agent/security_agent/policy.py',
            'stages/02-backend/deploy/postgres/001.sql')}
        selected = ps.protected_source_files(files, 'ops-agent')
        self.assertEqual(len(selected), 4)
        self.assertNotEqual(selected, ps.protected_source_files({}, 'ops-agent'))


class CapsuleTests(unittest.TestCase):
    def test_capsule_binds_candidate_images_baseline_and_both_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root/'powersystem-release-20261028T010000Z.tar.gz.gpg'
            archive.write_bytes(b'synthetic-encrypted')
            manifest = {'backup_tier': ps.STANDARD_TIER, 'release': 'patch-r1', 'parent': 'parent-r1',
                        'controller_sha256': ps.sha(ps.__file__), 'created_at': '2020-01-01T00:00:00+00:00',
                        'services': ['api'], 'images': {'api': 'sha256:'+'b'*64}}
            baseline = {'archive': 'powersystem-20261028T000000Z.tar.gz.gpg', 'sha256': 'fixed',
                        'snapshot_started_at': '2026-10-28T00:00:00+00:00'}
            receipt = {'status': 'ok', 'archive': archive.name, 'release': 'patch-r1', 'parent': 'parent-r1',
                       'manifest_sha256': ps.digest(manifest), 'images': manifest['images'],
                       'uploads': ['primary', 'secondary'], 'sha256': ps.sha(archive), 'baseline': baseline}
            pointer = {'release': 'parent-r1', 'published_at': '2026-10-27T00:00:00+00:00'}
            receipt_path = Path(str(archive)+'.receipt.json')
            with patch.object(ps, 'BACKUP_DIR', root), patch.object(ps, 'verified_full_baseline', return_value=baseline):
                ps.write(receipt_path, receipt)
                self.assertEqual(ps.verified_standard_capsule(archive, manifest, pointer)[1], baseline)
                for key, value in [('uploads', ['primary']), ('images', {}), ('parent', 'unrelated'),
                                   ('sha256', 'changed'), ('baseline', {**baseline, 'sha256': 'changed'})]:
                    ps.write(receipt_path, {**receipt, key: value})
                    with self.subTest(key=key), self.assertRaises(ps.Blocked):
                        ps.verified_standard_capsule(archive, manifest, pointer)
                # A later full deployment prevents reuse of an older data baseline.
                ps.write(receipt_path, receipt)
                pointer['published_at'] = '2026-10-29T00:00:00+00:00'
                with self.assertRaisesRegex(ps.Blocked, 'baseline_incompatible'):
                    ps.verified_standard_capsule(archive, manifest, pointer)

    def test_standard_capsule_contains_complete_sources_and_rejects_tampered_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root/'runtime/source-builds'
            names = set(sources.REQUIRED) | {recipe for _, recipe in sources.SERVICES.values()}
            source_manifest = {'format': 1, 'revision': 'a'*40,
                               'files': {name: {'sha256': sources.sha(b'synthetic-source'), 'mode': 420}
                                         for name in names}}
            source_hash = sources.digest(source_manifest)
            source = store/'contexts'/source_hash
            for name in names:
                path = source/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'synthetic-source')
            ps.write(source/'source-manifest.json', source_manifest)
            image = 'sha256:'+'b'*64
            receipt = store/'receipts'/(image[7:]+'.json')
            ps.write(receipt, {'image': image})
            candidate = root/'candidate'
            frozen = {'release': 'patch-r1', 'parent': 'parent-r1', 'services': ['api'],
                      'images': {'api': image}, 'controller_sha256': ps.sha(HERE/'production-stack.py'),
                      'files': {}, 'source_builds': [{'service': 'api', 'image': image,
                          'source_sha256': source_hash, 'receipt_sha256': ps.sha(receipt)}]}
            ps.write(candidate/'manifest.json', frozen)
            temp = root/'temp'
            temp.mkdir()
            def save_image(args, **kwargs):
                Path(args[3]).write_bytes(b'synthetic-image-tar')
            with patch.object(capsule.stack, 'ROOT', root), patch.object(capsule, 'checked', side_effect=save_image):
                packed = capsule.pack_standard(temp, candidate, frozen, {'archive': 'baseline'})
            metadata = ps.read(temp/'capsule.json')
            self.assertIn('application-images.tar', metadata['payload'])
            self.assertIn('source-builds/contexts/'+source_hash+'/source-manifest.json', metadata['payload'])
            data = packed.read_bytes()
            process = SimpleNamespace(stdout=io.BytesIO(data), wait=lambda timeout: 0)
            with patch.object(capsule.subprocess, 'Popen', return_value=process):
                self.assertEqual(capsule.verify_stream(packed)['images'], {'api': image})
            # Mutating a tool member while retaining the manifest must fail.
            changed = io.BytesIO()
            with capsule.tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as src, \
                    capsule.tarfile.open(fileobj=changed, mode='w:gz') as dst:
                for member in src:
                    body = src.extractfile(member).read()
                    if member.name == 'controller.py':
                        body += b'\n# unexpected'
                    member.size = len(body)
                    dst.addfile(member, io.BytesIO(body))
            process = SimpleNamespace(stdout=io.BytesIO(changed.getvalue()), wait=lambda timeout: 0)
            with patch.object(capsule.subprocess, 'Popen', return_value=process):
                with self.assertRaisesRegex(capsule.stack.Blocked, 'payload_mismatch'):
                    capsule.verify_stream(packed)


class EdgeScopeTests(unittest.TestCase):
    def run_gate(self, profile):
        seen = []
        def request(host, path, method='GET', body=None, token=None):
            seen.append((host, path, token is not None))
            if not token and method == 'GET':
                raise urllib.error.HTTPError('https://synthetic', 401, 'protected', {}, None)
            if path.endswith('/login'):
                return 200, {'code': 0, 'data': {'user': {'role': 'operator'}, 'token': 'synthetic'}}
            if path.endswith('ws-ticket'):
                return 200, {'code': 0, 'data': {'ticket': 'synthetic'}}
            return 200, {'code': 0, 'data': {'list': []}}
        with tempfile.TemporaryDirectory() as directory:
            password = Path(directory)/'synthetic-credential'
            password.write_text('synthetic', encoding='utf-8')
            with patch.object(edge.sys, 'argv', ['gate.py', str(password)]), \
                    patch.dict(edge.os.environ, {'POWERSYSTEM_GATE_PROFILE': profile,
                                                  'POWERSYSTEM_GATE_TIMING': ''}), \
                    patch.object(edge, 'request', side_effect=request), \
                    patch.object(edge, 'web_socket', return_value='HTTP/1.1 101 Switching Protocols'), \
                    contextlib.redirect_stdout(io.StringIO()):
                edge.main()
        return seen

    def test_standard_uses_main_domain_and_full_keeps_both_ip_entries(self):
        standard = self.run_gate('standard')
        self.assertEqual({r[0] for r in standard}, {'www.cyancarp.cn'})
        self.assertTrue(any(r[1] == '/api/v1/agent/tasks' for r in standard))
        self.assertTrue(any(r[1] == '/api/v1/ws-ticket' for r in standard))
        self.assertEqual({r[0] for r in self.run_gate('full')}, {'8.138.42.212', '8.138.10.222'})


class TransactionTests(unittest.TestCase):
    def test_standard_publish_uses_capsule_and_failure_keeps_original_rollback(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rows = {'api': {'Id': 'api-id', 'Image': 'sha256:'+'a'*64, 'RestartCount': 0,
                                'State': {'Running': True, 'StartedAt': 'same'}}}
                state = ps.states(rows)
                parent, candidate = root/'releases/parent-r1', root/'releases/patch-r1'
                ps.write(root/'active.json', {'release': 'parent-r1', 'gate_file': 'accepted.json'})
                ps.write(parent/'accepted.json', {'states': state})
                ps.write(candidate/'private/compose.json', {})
                m = {'parent': 'parent-r1', 'services': ['api'], 'backup_tier': ps.STANDARD_TIER,
                     'before': state, 'origin_environment': 'synthetic'}
                baseline = {'archive': 'baseline'}
                transition = Mock()
                transition.frozen.return_value = None
                with patch.object(ps, 'STATE_DIR', root), patch.object(ps, 'active', return_value=(parent, {})), \
                        patch.object(ps, 'immutable', return_value=m), patch.object(ps, 'telemetry_transition', return_value=transition), \
                        patch.object(ps, 'verified_standard_capsule', return_value=({'sha256': 'fixed'}, baseline)) as verify, \
                        patch.object(ps, 'inspect', return_value=rows), patch.object(ps, 'preflight'), \
                        patch.object(ps, 'drain'), patch.object(ps, 'deploy') as deploy, \
                        patch.object(ps, 'gate', side_effect=ps.Blocked('synthetic') if fail else None, return_value='accepted-new.json'), \
                        patch.object(ps, 'telemetry_monitoring', return_value=Mock()), \
                        patch.object(ps, 'set_active') as activate, patch.object(ps, 'restore_transaction') as rollback:
                    archive = root/'capsule'
                    archive.write_bytes(b'synthetic')
                    args = SimpleNamespace(id='patch-r1', backup=str(archive))
                    if fail:
                        with self.assertRaises(ps.Blocked):
                            ps.publish(args)
                        rollback.assert_called_once()
                        activate.assert_not_called()
                    else:
                        ps.publish(args)
                        rollback.assert_not_called()
                        activate.assert_called_once_with(candidate, 'accepted-new.json', 'baseline')
                        self.assertFalse((root/'pending.json').exists())
                    verify.assert_called_once()
                    deploy.assert_called_once_with(candidate, ['api'])

    def test_backup_reconciliation_checks_exact_receipt_without_redeploying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'releases/parent-r1'
            before = {s: {'id': s, 'image': 'fixed', 'restarts': 0, 'started_at': 'old'}
                      for s in ('api', 'agent-rag', 'ops-agent')}
            after = copy.deepcopy(before)
            for service in ('agent-rag', 'ops-agent'):
                after[service]['started_at'] = 'resumed'
            rows = {s: {'Id': r['id'], 'Image': r['image'], 'RestartCount': r['restarts'],
                        'State': {'Running': True, 'StartedAt': r['started_at']}} for s, r in after.items()}
            pointer = {'release': 'parent-r1', 'gate_file': 'old.json', 'published_at': '2026-10-27T00:00:00+00:00'}
            ps.write(root/'active.json', pointer)
            ps.write(path/'old.json', {'passed': True, 'states': before})
            archive = root/'full.tar.gz.gpg'
            baseline = {'archive': archive.name, 'sha256': 'fixed'}
            receipt = {'version': 1, 'active': pointer, **baseline, 'before': before, 'after': after}
            ps.write(Path(str(archive)+'.v4-maintenance.json'), receipt)
            with patch.object(ps, 'STATE_DIR', root), patch.object(ps, 'active', return_value=(path, {})), \
                    patch.object(ps, 'verified_full_baseline', return_value=baseline), \
                    patch.object(ps, 'inspect', return_value=rows), patch.object(ps, 'gate', return_value='maintenance.json') as gate, \
                    patch.object(ps, 'set_active') as activate, patch.object(ps, 'deploy') as deploy:
                ps.verify_backup_maintenance(SimpleNamespace(backup=str(archive), phase='reconcile-backup'))
                gate.assert_called_once_with(path, [], after, maintenance_only=True)
                self.assertEqual(activate.call_args.args[3], pointer['published_at'])
                deploy.assert_not_called()
                # A container replacement cannot be explained by maintenance.
                rows['api']['Id'] = 'unrelated-new-container'
                with self.assertRaisesRegex(ps.Blocked, 'post_resume_drift'):
                    ps.verify_backup_maintenance(SimpleNamespace(backup=str(archive), phase='reconcile-backup'))


if __name__ == '__main__':
    unittest.main()
