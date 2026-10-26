"""Regression tests for stale builds, incomplete contexts and forged receipts."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


sb = module('source_build_tests', 'source-build.py')
ps = module('source_build_controller_tests', 'production-stack.py')


class SourceBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)/'store'
        self.manifest = {'format': 1, 'revision': 'a'*40,
                         'reviewed_overlays': [], 'reviewed_deletions': [], 'files': {}}
        self.data = {name: ('fixture:'+name).encode() for name in
                     {*sb.REQUIRED, *(recipe for _, recipe in sb.SERVICES.values())}}
        self.data['stages/09-ops-agent/ops_agent/main.py'] = b'fixture ops'
        self.data['stages/04-frontend/obsolete.js'] = b'fixture old'
        self.make_snapshot()
        self.image = 'sha256:'+'b'*64
        self.metadata = {'Id': self.image, 'Config': {'Labels': sb.labels_for(self.info, 'api')}}

    def make_snapshot(self):
        self.manifest['files'] = {n: {'sha256': sb.sha(v), 'mode': 0o644}
                                  for n, v in self.data.items()}
        self.source = self.store/'contexts'/sb.digest(self.manifest)
        self.source.mkdir(parents=True)
        for name, value in self.data.items():
            path = self.source/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        sb.write_new(self.source/sb.MANIFEST, self.manifest)
        self.info = sb.verify(self.source)

    def receipt(self, **changes):
        value = {'format': 1, 'image': self.image, 'service': 'api',
                 'revision': self.info['revision'], 'source_sha256': self.info['sha256']}
        value.update(changes)
        sb.write_new(self.store/'receipts'/(self.image[7:]+'.json'), value)

    def test_deleted_v4_file_or_added_stale_file_blocks(self):
        target = self.source/sb.REQUIRED[0]
        target.unlink()
        with self.assertRaisesRegex(sb.SourceError, 'file_set_changed'):
            sb.verify(self.source)
        target.write_bytes(self.data[sb.REQUIRED[0]])
        (self.source/'stale.txt').write_text('unexpected')
        with self.assertRaisesRegex(sb.SourceError, 'file_set_changed'):
            sb.verify(self.source)

    def test_same_filename_with_old_bytes_blocks(self):
        (self.source/sb.REQUIRED[0]).write_bytes(b'pre-V4 source')
        with self.assertRaisesRegex(sb.SourceError, 'source_file_changed'):
            sb.verify(self.source)

    def test_ops_tar_contains_all_three_stage_contracts(self):
        packed, recipe = sb.build_tar(self.source, 'ops-agent')
        with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
            names = archive.getnames()
            self.assertIn('stages/09-ops-agent/ops_contract.py', names)
            self.assertIn('stages/10-security-agent/review_contract.py', names)
            self.assertIn('stages/08-agent-rag/rag_agent/run_api.py', names)
        self.assertEqual(recipe, 'stages/09-ops-agent/Dockerfile')

    def test_stage_tar_cannot_read_unmanifested_workspace(self):
        packed, recipe = sb.build_tar(self.source, 'frontend')
        with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
            self.assertIn('src/views/AgentSchedulesView.vue', archive.getnames())
            self.assertFalse(any(n.startswith('stages/') for n in archive.getnames()))
        self.assertEqual(recipe, 'Dockerfile')

    def test_labels_without_receipt_are_rejected(self):
        with self.assertRaisesRegex(sb.SourceError, 'receipt_missing'):
            sb.verify_receipt(self.image, 'api', self.store, self.metadata)

    def test_receipt_is_bound_to_image_service_and_source(self):
        self.receipt()
        sb.verify_receipt(self.image, 'api', self.store, self.metadata)
        with self.assertRaisesRegex(sb.SourceError, 'binding_mismatch'):
            sb.verify_receipt(self.image, 'frontend', self.store, self.metadata)
        changed = copy.deepcopy(self.metadata)
        changed['Config']['Labels'][sb.LABEL_SOURCE] = 'c'*64
        with self.assertRaisesRegex(sb.SourceError, 'labels_mismatch'):
            sb.verify_receipt(self.image, 'api', self.store, changed)
        (self.source/sb.REQUIRED[0]).write_bytes(b'tampered after build')
        with self.assertRaisesRegex(sb.SourceError, 'source_file_changed'):
            sb.verify_receipt(self.image, 'api', self.store, self.metadata)

    def test_private_paths_and_traversal_rejected(self):
        for name in ('../outside', 'stages/02-backend/private/token',
                     'stages/08-agent-rag/.env', 'stages/10-security-agent/key.pem'):
            with self.subTest(name=name), self.assertRaises(sb.SourceError):
                sb.safe_name(name)

    def test_export_refuses_unlisted_workspace_changes(self):
        with patch.object(sb, 'git', side_effect=[b'a'*40+b'\n',
                          b' M stages/02-backend/internal/api/agent_runs.go\0']):
            with self.assertRaisesRegex(sb.SourceError, 'unreviewed_workspace'):
                sb.export(self.temp.name, 'a'*40, self.store)

    def test_export_preserves_explicit_reviewed_addition_and_deletion(self):
        repo = Path(self.temp.name)/'repo'
        repo.mkdir()
        packed = io.BytesIO()
        with tarfile.open(fileobj=packed, mode='w') as archive:
            for name, data in self.data.items():
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = len(data), 0o644
                archive.addfile(entry, io.BytesIO(data))
        added = 'stages/04-frontend/src/new-reviewed.ts'
        deleted = 'stages/04-frontend/obsolete.js'
        (repo/added).parent.mkdir(parents=True)
        (repo/added).write_bytes(b'reviewed\r\n')
        dirty = ('?? '+added+'\0 D '+deleted+'\0').encode()
        with patch.object(sb, 'git', side_effect=[b'a'*40+b'\n', dirty, packed.getvalue()]):
            result = sb.export(repo, 'a'*40, Path(self.temp.name)/'other', [added], [deleted])
        source = Path(result['source'])
        self.assertEqual((source/added).read_bytes(), b'reviewed\n')
        self.assertFalse((source/deleted).exists())

    def test_controller_rejects_unattested_backend_before_candidate(self):
        old, new = 'sha256:'+'d'*64, self.image
        config = {'services': {'api': {'image': new}}}
        rows = {'api': {'Image': old}}
        with patch.object(ps, 'ROOT', Path(self.temp.name)), patch.object(ps, 'command',
                  return_value=json.dumps([self.metadata]).encode()):
            with self.assertRaisesRegex(ps.Blocked, 'source_build_required:api:source_build_receipt_missing'):
                ps.check_source_builds(config, rows, {'api'})

    def test_unchanged_accepted_images_do_not_need_retroactive_receipts(self):
        config = {'services': {'api': {'image': self.image}}}
        self.assertEqual(ps.check_source_builds(config, {'api': {'Image': self.image}}, {'api'}), [])

    def test_controller_accepts_verified_image_and_records_binding(self):
        self.receipt()
        config = {'services': {'api': {'image': self.image}}}
        root = Path(self.temp.name)/'production'
        (root/'runtime').mkdir(parents=True)
        # Keep the production path shape without requiring symlink privileges.
        import shutil
        shutil.copytree(self.store, root/'runtime/source-builds')
        with patch.object(ps, 'ROOT', root), patch.object(ps, 'command',
                  return_value=json.dumps([self.metadata]).encode()):
            result = ps.check_source_builds(config, {'api': {'Image': 'sha256:'+'d'*64}}, {'api'})
        self.assertEqual(result[0]['source_sha256'], self.info['sha256'])


if __name__ == '__main__':
    unittest.main()
