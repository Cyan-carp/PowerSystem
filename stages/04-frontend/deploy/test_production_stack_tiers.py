"""Small offline checks for the controlled frontend release tier."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'production_stack_tiers', Path(__file__).with_name('production-stack.py'))
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


class TierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        self.old_state, self.old_backup = ps.STATE_DIR, ps.BACKUP_DIR
        ps.STATE_DIR = self.root / 'state'
        ps.BACKUP_DIR = self.root / 'backups'
        ps.BACKUP_DIR.mkdir()

    def tearDown(self):
        ps.STATE_DIR, ps.BACKUP_DIR = self.old_state, self.old_backup
        self.temp.cleanup()

    def baseline(self, uploads=('upload_primary', 'upload_secondary')):
        archive = ps.BACKUP_DIR / 'powersystem-20261022T010000Z.tar.gz.gpg'
        archive.write_bytes(b'encrypted-full')
        Path(str(archive) + '.sha256').write_text(ps.sha(archive) + '  ' + archive.name)
        ps.write(Path(str(archive) + '.timing.json'), {
            'status': 'ok', 'archive': archive.name, 'started_at': ps.now(),
            'uploads': [{'name': name} for name in uploads]})
        return archive

    def test_full_baseline_requires_both_remote_uploads(self):
        archive = self.baseline(('upload_primary',))
        with self.assertRaisesRegex(ps.Blocked, 'remote_copies_unproven'):
            ps.verified_full_baseline(archive)

    def test_baseline_must_follow_nonfrontend_release(self):
        archive = self.baseline()
        pointer = {'updated_at': ps.now()}
        with self.assertRaisesRegex(ps.Blocked, 'must_follow_current_release'):
            ps.choose_fast_baseline(pointer)
        pointer['updated_at'] = '2020-01-01T00:00:00+00:00'
        self.assertEqual(ps.choose_fast_baseline(pointer)['archive'], archive.name)

    def test_baseline_must_be_within_24_hours(self):
        archive = self.baseline()
        import datetime
        receipt = Path(str(archive) + '.timing.json')
        value = ps.read(receipt)
        value['started_at'] = (datetime.datetime.now(datetime.timezone.utc) -
                               datetime.timedelta(hours=25)).isoformat()
        ps.write(receipt, value)
        with self.assertRaisesRegex(ps.Blocked, 'too_old'):
            ps.verified_full_baseline(archive)

    def test_frontend_only_rejects_config_and_scope_changes(self):
        parent = 'parent-r1'
        path = ps.release(parent)
        (path / 'private').mkdir(parents=True)
        old = {'services': {'frontend': {'image': 'old'}, 'api': {'image': 'same'}},
               'volumes': {'keep': {'name': 'keep'}}}
        ps.write(path / 'private/compose.json', old)
        ps.write(path / 'private/origin-args.json', ['compose', 'config'])
        ps.write(path / 'manifest.json', {'origin_environment': '/private/env'})
        changed = json.loads(json.dumps(old))
        changed['services']['frontend']['image'] = 'new'
        ps.validate_fast_frontend_config(changed, ['compose', 'config'], '/private/env',
                                         parent, {'frontend'}, {'frontend': 'new'})
        changed['volumes']['keep']['name'] = 'other'
        with self.assertRaisesRegex(ps.Blocked, 'configuration_changed'):
            ps.validate_fast_frontend_config(changed, ['compose', 'config'], '/private/env',
                                             parent, {'frontend'}, {'frontend': 'new'})
        with self.assertRaisesRegex(ps.Blocked, 'frontend_image_only'):
            ps.validate_fast_frontend_config(old, ['compose', 'config'], '/private/env',
                                             parent, {'frontend', 'api'}, {'frontend': 'new'})

    def test_capsule_must_match_candidate_and_baseline(self):
        baseline = self.baseline()
        pointer = {'updated_at': '2020-01-01T00:00:00+00:00'}
        selected = ps.choose_fast_baseline(pointer)
        capsule = ps.BACKUP_DIR / 'powersystem-frontend-20261022T020000Z.tar.gz.gpg'
        capsule.write_bytes(b'encrypted-capsule')
        manifest = {'release': 'new-r1', 'parent': 'parent-r1',
                    'created_at': '2020-01-01T00:00:00+00:00',
                    'controller_sha256': ps.sha(ps.__file__),
                    'images': {'frontend': 'sha256:' + 'a' * 64}}
        receipt = {'status': 'ok', 'archive': capsule.name, 'release': 'new-r1',
                   'parent': 'parent-r1', 'manifest_sha256': ps.digest(manifest),
                   'frontend_image': manifest['images']['frontend'],
                   'uploads': ['primary', 'secondary'], 'sha256': ps.sha(capsule),
                   'baseline': selected}
        path = Path(str(capsule) + '.receipt.json')
        ps.write(path, receipt)
        self.assertEqual(ps.verified_fast_capsule(capsule, manifest, pointer)[1], selected)
        receipt['baseline']['sha256'] = 'wrong'
        ps.write(path, receipt)
        with self.assertRaisesRegex(ps.Blocked, 'baseline_changed'):
            ps.verified_fast_capsule(capsule, manifest, pointer)


if __name__ == '__main__':
    unittest.main()
