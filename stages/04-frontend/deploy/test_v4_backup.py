"""Real temp-file negative tests for the V4 artifact snapshot CLI (no database)."""
import hashlib
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

spec = importlib.util.spec_from_file_location('v4_backup', Path(__file__).with_name('v4-backup.py'))
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def row_for(path, status='ready', data=None, **extra):
    data = path.read_bytes() if data is None else data
    row = dict(id=str(uuid4()), storage_key=path.name, size=len(data),
               sha256=hashlib.sha256(data).hexdigest(), status=status,
               expires_at='2026-10-30T00:00:00Z', file_removed_at=None)
    row.update(extra)
    return row


def evidence_file(directory, **overrides):
    data = {'writes_paused': True, 'scope': 'live',
            'paused_at': backup.iso(backup.now_utc()), 'reference': 'unit-test'}
    data.update(overrides)
    path = Path(directory) / ('evidence-' + uuid4().hex + '.json')
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


class ArtifactScanTests(unittest.TestCase):
    def test_ready_file_is_verified(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / str(uuid4()); path.write_bytes(b'real-file')
            result = backup.scan_files([row_for(path)], d, 1 << 20, 1 << 20)
            self.assertEqual((result['verified'], result['problems']), (1, []))
            self.assertEqual(result['files'][0]['file_state'], 'verified')

    def test_ready_missing_and_non_ready_missing_are_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / str(uuid4())
            ready = backup.scan_files([row_for(path, data=b'x')], d, 1 << 20, 1 << 20)
            self.assertEqual(ready['problems'][0]['reason'], 'ready_file_missing')
            expired = backup.scan_files([row_for(path, status='expired', data=b'x')], d, 1 << 20, 1 << 20)
            self.assertEqual((expired['problems'], expired['absent_expected']), ([], 1))

    def test_size_and_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / str(uuid4()); path.write_bytes(b'corrupt!!!')
            row = row_for(path, data=b'expected!')
            result = backup.scan_files([row], d, 1 << 20, 1 << 20)
            reasons = {item['reason'] for item in result['problems']}
            self.assertIn('size_mismatch', reasons)
            self.assertIn('digest_mismatch', reasons)
            self.assertEqual(result['files'][0]['file_state'], 'mismatch')

    def test_unavailable_mismatch_is_recorded_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / str(uuid4()); path.write_bytes(b'corrupt!!')
            row = row_for(path, status='unavailable', data=b'expected!')
            result = backup.scan_files([row], d, 1 << 20, 1 << 20)
            self.assertEqual(result['problems'], [])
            self.assertEqual(result['files'][0]['file_state'], 'unavailable_mismatch_expected')

    def test_path_traversal_and_noncanonical_keys(self):
        with tempfile.TemporaryDirectory() as d:
            for key in ('../escape', str(uuid4()).upper(), '/absolute', 'not-a-uuid'):
                row = dict(id='test', storage_key=key, size=0,
                           sha256=hashlib.sha256(b'').hexdigest(), status='ready')
                result = backup.scan_files([row], d, 1 << 20, 1 << 20)
                self.assertIn('storage_key_not_canonical',
                              {item['reason'] for item in result['problems']})

    def test_duplicate_keys_and_ids_fail_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / str(uuid4()); path.write_bytes(b'x')
            row = row_for(path)
            duplicate = dict(row)
            problems = backup.validate_rows([row, duplicate])
            reasons = {item['reason'] for item in problems}
            self.assertIn('duplicate_id', reasons)
            self.assertIn('duplicate_storage_key', reasons)

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / str(uuid4()); target.write_bytes(b'x')
            link = Path(d) / str(uuid4())
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks unavailable')
            row = dict(id=str(uuid4()), storage_key=link.name, size=1,
                       sha256=hashlib.sha256(b'x').hexdigest(), status='ready')
            result = backup.scan_files([row], d, 1 << 20, 1 << 20)
            self.assertIn('path_escape_or_symlink',
                          {item['reason'] for item in result['problems']})

    def test_unreferenced_file_and_directory_fail(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / str(uuid4()); path.write_bytes(b'x')
            (Path(d) / 'stray.bin').write_bytes(b'y')
            (Path(d) / 'stray-dir').mkdir()
            result = backup.scan_files([row_for(path)], d, 1 << 20, 1 << 20)
            reasons = {item['reason'] for item in result['problems']}
            self.assertEqual(reasons, {'unreferenced_file', 'unexpected_directory'})

    def test_per_file_limit(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / str(uuid4()); path.write_bytes(b'12345')
            result = backup.scan_files([row_for(path)], d, 4, 1 << 20)
            self.assertIn('size_limit_exceeded',
                          {item['reason'] for item in result['problems']})

    def test_file_present_after_removal_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / str(uuid4()); path.write_bytes(b'x')
            row = row_for(path, status='expired', file_removed_at='2026-10-24T00:00:00Z')
            result = backup.scan_files([row], d, 1 << 20, 1 << 20)
            self.assertIn('file_present_after_removal',
                          {item['reason'] for item in result['problems']})


class EvidenceAndOutputTests(unittest.TestCase):
    def test_pause_evidence_must_be_explicit_and_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            good = evidence_file(d)
            self.assertEqual(backup.load_evidence(good)['scope'], 'live')
            for overrides, code in (({'writes_paused': False}, 'writes_not_paused'),
                                    ({'scope': 'unknown'}, 'pause_scope_invalid'),
                                    ({'paused_at': '2020-01-01T00:00:00Z'}, 'pause_evidence_stale')):
                path = evidence_file(d, **overrides)
                with self.subTest(code=code), self.assertRaises(backup.Fail) as caught:
                    backup.load_evidence(path)
                self.assertEqual(caught.exception.code, code)
            with self.assertRaises(backup.Fail) as caught:
                backup.load_evidence(Path(d) / 'missing.json')
            self.assertEqual(caught.exception.code, 'pause_evidence_unreadable')

    def test_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            existing = Path(d) / 'exists'; existing.mkdir()
            with self.assertRaises(backup.Fail) as caught:
                backup.new_output_dir(existing, d)
            self.assertEqual(caught.exception.code, 'output_exists')
            target = Path(d) / 'file'
            target.write_text('keep', encoding='utf-8')
            with self.assertRaises(backup.Fail) as caught:
                backup.write_new_file(target, 'replace')
            self.assertEqual(caught.exception.code, 'output_exists')
            self.assertEqual(target.read_text(encoding='utf-8'), 'keep')

    def test_output_inside_artifact_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(backup.Fail) as caught:
                backup.new_output_dir(Path(d) / 'manifest-out', d)
            self.assertEqual(caught.exception.code, 'output_inside_artifact_dir')

    def test_capture_requires_pause_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / str(uuid4()); path.write_bytes(b'x')
            dsn = Path(d) / 'dsn'; dsn.write_text('postgresql://u:p@127.0.0.1/rag', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                code = backup.main(['capture', '--database-url-file', str(dsn),
                                    '--artifact-dir', d, '--output', str(Path(d).parent / ('out-' + uuid4().hex))])
            self.assertEqual(code, 2)

    def test_dsn_fingerprint_excludes_userinfo(self):
        first = backup.dsn_fingerprint('postgresql://u:one@db.example:5432/rag')
        second = backup.dsn_fingerprint('postgresql://other:two@db.example:5432/rag')
        self.assertEqual(first, second)
        self.assertNotIn('one', first)

    def test_dsn_loader_rejects_multiline_and_accepts_options(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'dsn'
            path.write_text('postgresql://u:p@127.0.0.1/rag?sslmode=disable', encoding='utf-8')
            dsn, database = backup.load_dsn(path)
            self.assertEqual(database, 'rag')
            evidence = evidence_file(d)
            output = Path(d).parent / ('out-' + uuid4().hex)
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                code = backup.main(['capture', '--database-url-file', str(Path(d) / 'missing'),
                                    '--artifact-dir', d, '--pause-evidence', str(evidence),
                                    '--output', str(output)])
            self.assertEqual(code, 2)
            self.assertNotIn('u:p', printed.getvalue())
            path.write_text('postgresql://u:p@127.0.0.1/rag\nsecond', encoding='utf-8')
            with self.assertRaises(backup.Fail) as caught:
                backup.load_dsn(path)
            self.assertEqual(caught.exception.code, 'database_url_file_invalid')


class CompareTests(unittest.TestCase):
    def _expected(self, key):
        return {'artifacts': {'rows': 1, 'table_sha256': 'table', 'files': [{
            'storage_key': key, 'metadata_sha256': 'meta', 'file_state': 'verified',
            'file_size': 3, 'file_sha256': 'sha'}]}}

    def test_identical_manifest_has_no_differences(self):
        key = str(uuid4())
        scan = {'files': [{'storage_key': key, 'metadata_sha256': 'meta', 'file_state': 'verified',
                           'file_size': 3, 'file_sha256': 'sha'}], 'problems': []}
        snap = {'count': 1, 'table_sha256': 'table'}
        self.assertEqual(backup.compare(self._expected(key), snap, scan), [])

    def test_malformed_manifest_does_not_crash(self):
        self.assertEqual(
            backup.compare({}, {'count': 0, 'table_sha256': 'x'}, {'files': [], 'problems': []})[0]['reason'],
            'manifest_artifacts_invalid')

    def test_missing_metadata_and_file_drift_are_distinct(self):
        key = str(uuid4())
        scan = {'files': [{'storage_key': key, 'metadata_sha256': 'other', 'file_state': 'mismatch',
                           'file_size': 4, 'file_sha256': 'other'}], 'problems': []}
        snap = {'count': 2, 'table_sha256': 'changed'}
        reasons = {item['reason'] for item in backup.compare(self._expected(key), snap, scan)}
        self.assertEqual(reasons, {'row_count_mismatch', 'table_digest_mismatch', 'metadata_mismatch',
                                   'file_state_mismatch', 'file_size_mismatch', 'file_digest_mismatch'})
        missing = backup.compare(self._expected(str(uuid4())), snap, {'files': [], 'problems': []})
        self.assertIn('artifact_missing_now', {item['reason'] for item in missing})


if __name__ == '__main__':
    unittest.main()
