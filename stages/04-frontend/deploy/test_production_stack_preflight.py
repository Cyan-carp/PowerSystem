"""Offline checks for the release controller preflight and stage timing.

No Docker, no credentials, no production state: every external call is replaced.
Run from the repository root:
  python -m unittest discover -s stages/04-frontend/deploy -p 'test_production_stack*.py'
"""
import importlib.util
import json
import pathlib
import tempfile
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'production_stack', Path(__file__).with_name('production-stack.py'))
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)

MIGRATIONS = Path(__file__).resolve().parents[2] / '02-backend/deploy/postgres'


class MigrationSplitTests(unittest.TestCase):
    def test_current_migrations_pass_the_real_splitter(self):
        files = sorted(MIGRATIONS.glob('*.sql'))
        self.assertGreaterEqual(len(files), 11)
        for path in files:
            with self.subTest(name=path.name):
                self.assertEqual(ps.migration_problems(path.name, path.read_text(encoding='utf-8')), [])

    def test_semicolon_inside_comment_is_rejected(self):
        # The 2026-10-21 r1 failure: a comment kept a ';', so the text after it
        # became a broken statement and the new API container could not start.
        content = ('ALTER TABLE alarm_records ADD COLUMN IF NOT EXISTS note text;\n'
                   '-- never reset the counter; it is monotonic\n'
                   'CREATE INDEX IF NOT EXISTS idx_note ON alarm_records(note);\n')
        problems = ps.migration_problems('010_fixture.sql', content)
        self.assertTrue(any('semicolon_in_comment' in item for item in problems), problems)
        self.assertTrue(any('unsupported_statement' in item for item in problems), problems)

    def test_clean_comment_without_semicolon_is_accepted(self):
        content = ('-- monotonic counter, never reset\n'
                   'CREATE TABLE IF NOT EXISTS t (id bigserial primary key);\n')
        self.assertEqual(ps.migration_problems('011_fixture.sql', content), [])

    def test_dollar_quote_is_rejected(self):
        content = 'CREATE FUNCTION f() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;\n'
        self.assertTrue(any('dollar_quote' in item
                            for item in ps.migration_problems('012.sql', content)))

    def test_semicolon_inside_string_literal_is_rejected(self):
        content = "INSERT INTO t(note) VALUES ('a;b') ON CONFLICT DO NOTHING;\n"
        self.assertTrue(ps.migration_problems('013.sql', content))

    def test_comment_marker_inside_literal_is_not_stripped(self):
        self.assertEqual(ps.executable("SELECT 'a--b'"), "SELECT 'a--b'")
        self.assertEqual(ps.executable("SELECT 1 -- trailing\n"), 'SELECT 1')


class PrivateLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'opt/powersystem'
        self.original = ps.ROOT
        ps.ROOT = self.root
        self.batch = self.root / 'runtime/ma-m4-test-r1'
        self.executor = self.batch / 'private/executor'
        self.executor.mkdir(parents=True)
        (self.executor / 'executor.json').write_text('{}', encoding='utf-8')
        self.env = self.batch / 'private/release.env'
        self.env.write_text('A=1\n', encoding='utf-8')

    def tearDown(self):
        ps.ROOT = self.original
        self.temporary.cleanup()

    def config(self, source):
        return {'services': {'ops-executor': {'volumes': [
            {'type': 'bind', 'source': str(source), 'target': '/run/ma-executor'}]}}}

    def test_batch_private_layout_is_accepted(self):
        value = ps.check_private_layout(self.config(self.executor), self.env)
        self.assertEqual(value['batch'], 'ma-m4-test-r1')

    def test_env_outside_batch_private_is_rejected(self):
        stray = self.root / 'runtime/other/private/release.env'
        stray.parent.mkdir(parents=True)
        stray.write_text('A=1\n', encoding='utf-8')
        with self.assertRaises(ps.Blocked) as raised:
            ps.check_private_layout(self.config(self.executor), stray)
        self.assertIn('private_executor_directory_missing', str(raised.exception))

    def test_env_not_under_private_is_rejected(self):
        flat = self.batch / 'release.env'
        flat.write_text('A=1\n', encoding='utf-8')
        with self.assertRaises(ps.Blocked) as raised:
            ps.check_private_layout(self.config(self.executor), flat)
        self.assertIn('private_env_outside_batch_private', str(raised.exception))

    def test_executor_mount_outside_batch_is_rejected(self):
        other = self.batch / 'private/elsewhere'
        other.mkdir()
        with self.assertRaises(ps.Blocked) as raised:
            ps.check_private_layout(self.config(other), self.env)
        self.assertIn('executor_mount_outside_batch', str(raised.exception))

    def test_missing_executor_directory_is_rejected(self):
        (self.executor / 'executor.json').unlink()
        self.executor.rmdir()
        with self.assertRaises(ps.Blocked) as raised:
            ps.check_private_layout(self.config(self.executor), self.env)
        self.assertIn('private_executor_directory_missing', str(raised.exception))


class AssetLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger = Path(self.temporary.name) / 'registry.json'
        self.original = ps.command

    def tearDown(self):
        ps.command = self.original
        self.temporary.cleanup()

    def write(self, versions, identities=None):
        assets = [{'asset_id': name, 'kind': 'container', 'version': version}
                  for name, version in versions.items()]
        assets.append({'asset_id': 'r730xd-host', 'kind': 'host', 'version': 9})
        self.ledger.write_text(json.dumps({
            'assets': assets,
            'private_assets': identities if identities is not None else
            {name: {'identity': {}} for name in versions}}), encoding='utf-8')

    def commit(self, versions):
        ps.command = lambda args, log=None, env=None: json.dumps(versions).encode()

    def config(self):
        return {'services': {'ops-executor': {'volumes': []}}}

    def test_matching_versions_pass(self):
        self.write({'r730xd-api': 4})
        self.commit({'r730xd-api': 4})
        value = ps.check_asset_ledger(self.config(), self.ledger)
        self.assertEqual(value['containers'], 1)
        self.assertEqual(value['assets'], 2)

    def test_one_step_ahead_is_an_uncommitted_import(self):
        self.write({'r730xd-api': 5})
        self.commit({'r730xd-api': 4})
        ps.check_asset_ledger(self.config(), self.ledger)

    def test_version_jump_is_rejected(self):
        # 2026-10-21 r2: repeated failed imports pushed the host ledger to
        # committed+3, and the next publish was refused by the version rule.
        self.write({'r730xd-api': 7})
        self.commit({'r730xd-api': 4})
        with self.assertRaises(ps.Blocked) as raised:
            ps.check_asset_ledger(self.config(), self.ledger)
        self.assertIn('asset_version_ahead_of_database:r730xd-api', str(raised.exception))

    def test_missing_identity_is_rejected(self):
        self.write({'r730xd-api': 1}, identities={})
        self.commit({'r730xd-api': 1})
        with self.assertRaises(ps.Blocked) as raised:
            ps.check_asset_ledger(self.config(), self.ledger)
        self.assertIn('asset_identity_missing', str(raised.exception))

    def test_unreadable_ledger_query_is_rejected(self):
        self.write({'r730xd-api': 1})

        def broken(args, log=None, env=None):
            raise ps.Blocked('command_failed:docker')

        ps.command = broken
        with self.assertRaises(ps.Blocked) as raised:
            ps.check_asset_ledger(self.config(), self.ledger)
        self.assertIn('asset_ledger_unreadable', str(raised.exception))


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.original = ps.TIMELINE
        ps.TIMELINE = ps.Timeline('publish', 'test-release')

    def tearDown(self):
        ps.TIMELINE = self.original

    def test_span_records_seconds_and_bytes(self):
        with ps.track('deploy.compose_up') as span:
            span['bytes'] = 1234
            time.sleep(0.01)
        recorded = ps.TIMELINE.spans[-1]
        self.assertEqual(recorded['name'], 'deploy.compose_up')
        self.assertEqual(recorded['bytes'], 1234)
        self.assertTrue(recorded['ok'])
        self.assertGreaterEqual(recorded['seconds'], 0.0)
        self.assertGreaterEqual(span['seconds'], 0.0)

    def test_failed_span_is_recorded_and_reraised(self):
        with self.assertRaises(ps.Blocked):
            with ps.track('gate.https_wss'):
                raise ps.Blocked('preflight:asset_registry_missing')
        recorded = ps.TIMELINE.spans[-1]
        self.assertFalse(recorded['ok'])
        self.assertEqual(recorded['error'], 'preflight:asset_registry_missing')

    def test_describe_keeps_slowest_first(self):
        for name, pause in (('fast', 0.0), ('slow', 0.02)):
            with ps.track(name):
                time.sleep(pause)
        value = ps.TIMELINE.describe()
        self.assertEqual(value['slowest'][0]['name'], 'slow')
        self.assertEqual(value['phase'], 'publish')

    def test_backup_summary_reads_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'powersystem-20261022T000000Z.tar.gz.gpg'
            archive.write_bytes(b'\0' * 32)
            sidecar = Path(str(archive) + '.timing.json')
            sidecar.write_text(json.dumps({
                'status': 'ok', 'total_seconds': 321.5, 'archive_bytes': 32,
                'uploads': [{'name': 'upload_primary', 'seconds': 100.0,
                             'bytes': 32000000, 'megabytes_per_second': 0.32}],
                'spans': []}), encoding='utf-8')
            value = ps.backup_summary(archive)
            self.assertEqual(value['total_seconds'], 321.5)
            self.assertEqual(value['uploads'][0]['megabytes_per_second'], 0.32)

    def test_missing_sidecar_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'powersystem-20261022T000000Z.tar.gz.gpg'
            archive.write_bytes(b'')
            self.assertIsNone(ps.backup_summary(archive))


class TimingHelperTests(unittest.TestCase):
    """backup-timing.py must stay in sync with the JSON lines backup.sh writes."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'backup_timing', Path(__file__).with_name('backup-timing.py'))
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)

    def test_summary_reports_upload_throughput(self):
        stages = [
            {'name': 'postgres_dump', 'microseconds': 2_000_000, 'offset_microseconds': 1_000_000,
             'bytes': 500},
            {'name': 'upload_primary', 'microseconds': 60_000_000, 'offset_microseconds': 90_000_000,
             'bytes': 650_003_238},
            {'name': 'upload_secondary', 'microseconds': 30_000_000, 'offset_microseconds': 150_000_000,
             'bytes': 650_003_238},
        ]
        value = self.helper.summarise(stages, run_id='powersystem-test', host='R730XD')
        self.assertEqual([item['name'] for item in value['uploads']],
                         ['upload_primary', 'upload_secondary'])
        self.assertAlmostEqual(value['uploads'][1]['megabytes_per_second'], 21.67, places=2)
        self.assertEqual(value['total_seconds'], 180.0)
        self.assertEqual(value['slowest'][0]['name'], 'upload_primary')

    def test_failed_run_keeps_stage_names(self):
        value = self.helper.summarise([], run_id='powersystem-test', status='failed',
                                      failed_stage='tdengine_dump', failed_line='57')
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['failed_stage'], 'tdengine_dump')
        self.assertEqual(value['total_seconds'], 0.0)
        self.assertEqual(value['uploads'], [])


if __name__ == '__main__':
    unittest.main()
