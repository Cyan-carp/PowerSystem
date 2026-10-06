import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('source_sync',Path(__file__).resolve().parents[1]/'deploy/sync-v2m4-source.py')
sync=importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)


class SourceSyncTest(unittest.TestCase):
    def fixture(self, directory, candidate_code=b'original-code'):
        root=Path(directory)/'server';root.mkdir()
        code=root/'stages/07-agent/agent/main.py';code.parent.mkdir(parents=True);code.write_bytes(b'original-code')
        (root/'README.md').write_bytes(b'original-note')
        archive=Path(directory)/'candidate.tar.gz';rows=[]
        with tarfile.open(archive,'w:gz') as package:
            for name,raw in [('README.md',b'reviewed-note'),('stages/07-agent/agent/main.py',candidate_code)]:
                item=tarfile.TarInfo(name);item.size=len(raw);package.addfile(item,io.BytesIO(raw))
                rows.append({'path':name,'sha256':hashlib.sha256(raw).hexdigest()})
        manifest=Path(directory)/'candidate.json'
        manifest.write_text(json.dumps({'commit':'reviewed-commit','base':'base-commit','archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'files':rows}))
        baseline=Path(directory)/'runtime.json';baseline.write_text(json.dumps([{'path':code.relative_to(root).as_posix(),'sha256':hashlib.sha256(b'original-code').hexdigest()}]))
        return SimpleNamespace(root=root,archive=archive,manifest=manifest,runtime_manifest=baseline,output=Path(directory)/'proof.json')

    def test_notes_sync_preserves_running_code(self):
        with tempfile.TemporaryDirectory() as directory:
            args=self.fixture(directory);sync.main(args)
            report=json.loads(args.output.read_text())
            self.assertTrue(report['passed']);self.assertFalse(report['running_assets_changed']);self.assertFalse(report['services_restarted'])
            self.assertEqual((args.root/'README.md').read_bytes(),b'reviewed-note')
            self.assertEqual((args.root/'stages/07-agent/agent/main.py').read_bytes(),b'original-code')

    def test_runtime_change_rejected_before_note_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            args=self.fixture(directory,b'changed-code')
            with self.assertRaises(AssertionError):sync.main(args)
            self.assertEqual((args.root/'README.md').read_bytes(),b'original-note');self.assertFalse(args.output.exists())

    def test_server_runtime_drift_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            args=self.fixture(directory);(args.root/'stages/07-agent/agent/main.py').write_bytes(b'unknown-server-code')
            with self.assertRaises(AssertionError):sync.main(args)
            self.assertEqual((args.root/'README.md').read_bytes(),b'original-note')

    def test_package_digest_and_existing_output_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            args=self.fixture(directory);args.archive.write_bytes(args.archive.read_bytes()+b'tampered')
            with self.assertRaises(AssertionError):sync.main(args)
            self.assertEqual((args.root/'README.md').read_bytes(),b'original-note')
            args.output.write_text('existing-proof')
            with self.assertRaises(FileExistsError):sync.main(args)
            self.assertEqual(args.output.read_text(),'existing-proof')
