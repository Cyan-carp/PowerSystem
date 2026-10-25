"""Pure/static tests for the V4 backup integration glue (no docker, no database)."""
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('v4_backup_integration', HERE / 'v4-backup-integration.py')
glue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(glue)

backup_spec = importlib.util.spec_from_file_location('v4_backup_for_integration', HERE / 'v4-backup.py')
backup = importlib.util.module_from_spec(backup_spec)
backup_spec.loader.exec_module(backup)


def inspect_doc(env=None, running=True, mounts=None, name='/rag-1'):
    return {'Id': 'synthetic-'+name, 'Name': name, 'State': {'Running': running},
            'Config': {'Env': [f'{key}={value}' for key, value in (env or {}).items()]},
            'Mounts': mounts or []}


def bind(source):
    return {'Type': 'bind', 'Source': str(source), 'Destination': '/artifacts', 'RW': True}


class ProbeTests(unittest.TestCase):
    def test_flags_are_case_insensitive_and_either_switch_activates(self):
        self.assertTrue(glue.v4_active({'V4_RUNS_ENABLED': 'TRUE'}))
        self.assertTrue(glue.v4_active({'V4_ARTIFACTS_ENABLED': '1'}))
        self.assertFalse(glue.v4_active({'V4_RUNS_ENABLED': 'false'}))

    def test_valid_runtime_bind_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(os.path.realpath(d))
            project = root / 'proj'
            source = project / 'runtime' / 'rag-artifacts'
            source.mkdir(parents=True)
            self.assertEqual(glue.artifact_bind(inspect_doc(mounts=[bind(source)]), project),
                             str(source))

    def test_bind_outside_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(os.path.realpath(d))
            project = root / 'proj'
            outside = project / 'other'
            outside.mkdir(parents=True)
            with self.assertRaises(glue.Fail) as caught:
                glue.artifact_bind(inspect_doc(mounts=[bind(outside)]), project)
            self.assertEqual(caught.exception.code, 'artifacts_bind_outside_runtime')

    def test_missing_ambiguous_and_non_bind_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(os.path.realpath(d))
            project = root / 'proj'
            source = project / 'runtime' / 'a'
            source.mkdir(parents=True)
            for mounts, code in (([], 'artifacts_bind_missing'),
                                 ([bind(source), bind(source)], 'artifacts_bind_ambiguous'),
                                 ([dict(bind(source), Type='volume')], 'artifacts_bind_not_bind')):
                with self.subTest(code=code), self.assertRaises(glue.Fail) as caught:
                    glue.artifact_bind(inspect_doc(mounts=mounts), project)
                self.assertEqual(caught.exception.code, code)

    def test_symlinked_bind_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(os.path.realpath(d))
            project = root / 'proj'
            real = project / 'runtime' / 'real'
            real.mkdir(parents=True)
            link = project / 'runtime' / 'link'
            try:
                os.symlink(real, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks unavailable')
            with self.assertRaises(glue.Fail) as caught:
                glue.artifact_bind(inspect_doc(mounts=[bind(link)]), project)
            self.assertEqual(caught.exception.code, 'artifacts_bind_symlink')

    def test_probe_inactive_does_not_require_bind(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'inspect.json'
            path.write_text(json.dumps([inspect_doc(env={'V4_RUNS_ENABLED': 'false'})]), encoding='utf-8')
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(glue.main(['probe', '--inspect', str(path), '--project-dir', d]), 0)
            self.assertFalse(json.loads(out.getvalue())['active'])


class EvidenceTests(unittest.TestCase):
    def state(self, rag=True, ops=False):
        return {'rag': {'container': 'rag-1', 'id':'synthetic-/rag-1','running': rag},
                'ops': {'container': 'ops-1' if ops else '', 'id':'synthetic-/ops-1' if ops else None,'running': ops}}

    def test_evidence_requires_both_writers_stopped(self):
        with self.assertRaises(glue.Fail) as caught:
            glue.build_evidence(inspect_doc(running=True), None, self.state(),
                                'live', 'run-1', glue.now_utc(), 1)
        self.assertEqual(caught.exception.code, 'rag_still_running')
        with self.assertRaises(glue.Fail) as caught:
            glue.build_evidence(inspect_doc(running=False),
                                inspect_doc(running=True, name='/ops-1'),
                                self.state(), 'live', 'run-1', glue.now_utc(), 1)
        self.assertEqual(caught.exception.code, 'ops_still_running')

    def test_evidence_records_original_running_state_only(self):
        value = glue.build_evidence(inspect_doc(running=False), None, self.state(),
                                    'live', 'run-1', glue.now_utc(), 2)
        self.assertTrue(value['writes_paused'])
        self.assertEqual(value['scope'], 'live')
        self.assertTrue(value['writers']['rag']['was_running'])
        self.assertFalse(value['writers']['ops']['was_running'])
        self.assertEqual(glue.resume_names(self.state()), ['rag-1'])

    def test_resume_never_starts_writers_that_were_not_running(self):
        self.assertEqual(glue.resume_names(self.state(rag=False, ops=False)), [])
        self.assertEqual(glue.resume_names(self.state(rag=False, ops=True)), ['ops-1'])

    def test_cli_round_trip_writes_private_evidence_and_resume_names(self):
        with tempfile.TemporaryDirectory() as d:
            rag_before = Path(d) / 'rag-before.json'
            rag_after = Path(d) / 'rag-after.json'
            state = Path(d) / 'state.json'
            evidence = Path(d) / 'evidence.json'
            rag_before.write_text(json.dumps([inspect_doc(env={'V4_RUNS_ENABLED': 'true'},
                                                         name='/rag-1')]), encoding='utf-8')
            rag_after.write_text(json.dumps([inspect_doc(running=False, name='/rag-1')]), encoding='utf-8')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(glue.main(['state', '--rag-inspect', str(rag_before),
                                            '--output', str(state)]), 0)
                self.assertEqual(glue.main(['evidence', '--rag-inspect', str(rag_after),
                                            '--state', str(state), '--output', str(evidence),
                                            '--run-id', 'run-1']), 0)
            if os.name != 'nt':
                self.assertEqual(oct(evidence.stat().st_mode & 0o777), '0o600')
            self.assertTrue(json.loads(evidence.read_text(encoding='utf-8'))['writes_paused'])
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(glue.main(['resume', '--state', str(state)]), 0)
            self.assertEqual(out.getvalue().strip(), 'rag-1')


class CaptureContractTests(unittest.TestCase):
    def test_capture_accepts_psql_exported_rows_shape(self):
        import hashlib
        from uuid import uuid4
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            key = str(uuid4())
            data = b'artifact-bytes'
            (root / 'files').mkdir()
            (root / 'files' / key).write_bytes(data)
            rows = root / 'rows.json'
            rows.write_text(json.dumps({'database': 'powersystem_agent_rag', 'rows': [{
                'id': str(uuid4()), 'storage_key': key, 'size': len(data),
                'sha256': hashlib.sha256(data).hexdigest(), 'status': 'ready',
                'expires_at': '2026-10-30T00:00:00Z', 'file_removed_at': None,
                'provenance': {'call': 'x'}}]}), encoding='utf-8')
            evidence = root / 'evidence.json'
            evidence.write_text(json.dumps({'writes_paused': True, 'scope': 'live',
                                            'paused_at': backup.iso(backup.now_utc())}),
                                encoding='utf-8')
            with redirect_stdout(io.StringIO()) as out:
                code = backup.main(['capture', '--rows-file', str(rows),
                                    '--artifact-dir', str(root / 'files'),
                                    '--pause-evidence', str(evidence),
                                    '--expect-database', 'powersystem_agent_rag',
                                    '--output', str(root / 'manifest')])
            self.assertEqual(code, 0, out.getvalue())
            manifest = json.loads((root / 'manifest' / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['database']['input'], 'postgres_export')
            self.assertEqual(manifest['artifacts']['verified'], 1)


class ShellIntegrationTests(unittest.TestCase):
    def read(self, name):
        return (HERE / name).read_text(encoding='utf-8')

    def test_backup_pauses_before_dump_and_skips_duplicate_vectors(self):
        text = self.read('backup.sh')
        self.assertLess(text.index('v4_pause'), text.index('stage_begin postgres_dump'))
        self.assertLess(text.index('v4_rag_vectors'), text.index('stage_begin v4_pause'))
        self.assertIn('if [ "$rag_vectors_done" = 0 ]', text)
        self.assertIn('--rows-file "$staging/v4-artifacts.rows.json"', text)
        self.assertIn('--pause-evidence "$staging/v4-pause-evidence.json"', text)
        self.assertIn('v4_resume', text[text.index('finish() {'):])
        self.assertIn('resume --state "$v4_state_file"', text)
        self.assertIn("json_build_object('database', current_database(), 'rows'", text)
        self.assertIn('cp -a "$v4_artifact_root/." "$staging/v4-artifacts/"', text)

    def test_restore_verifies_v4_offline_before_any_worker_start(self):
        text = self.read('restore-verify.sh')
        self.assertIn('verify --offline', text)
        self.assertIn('--rows-file "$restore_root/$run_id/v4-artifacts.restored.rows.json"', text)
        self.assertIn("CREATE DATABASE powersystem_agent_rag", text)
        self.assertLess(text.index('v4_offline_verify=ok'), text.index('--profile agent-restore up'))
        self.assertNotIn('scheduler', text[text.index('v4_offline_verify=ok'):].lower())


if __name__ == '__main__':
    unittest.main()
