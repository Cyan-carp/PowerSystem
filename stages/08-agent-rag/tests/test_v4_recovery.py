"""Recovery CLI boundaries; real PG/file restoration is a separate acceptance."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

spec = importlib.util.spec_from_file_location('v4_recovery', Path(__file__).resolve().parents[1]/'deploy/v4-recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryBoundaryTests(unittest.TestCase):
    def test_dsn_query_cannot_override_checked_host_or_database(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'url'
            p.write_text('postgresql://u:p@127.0.0.1:25439/v4_s09_rag_test')
            self.assertTrue(recovery.load_url(p))
            for suffix in ('?host=production', '?dbname=powersystem', '#ignored'):
                p.write_text('postgresql://u:p@127.0.0.1/v4_s09_rag_test'+suffix)
                with self.subTest(suffix=suffix), self.assertRaises(recovery.Fail):
                    recovery.load_url(p)

    def test_actual_file_integrity_and_ready_missing_are_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            key=str(uuid4());p=Path(d)/key;p.write_bytes(b'real-file')
            row=dict(id=str(uuid4()),storage_key=key,size=9,sha256=hashlib.sha256(b'real-file').hexdigest(),status='ready')
            self.assertEqual(recovery.check_files([row],d)['checked'],1)
            p.write_bytes(b'corrupt!!')
            self.assertEqual(recovery.check_files([row],d)['problems'][0]['reason'],'size_or_digest_mismatch')
            row['storage_key']=str(uuid4())
            self.assertEqual(recovery.check_files([row],d)['problems'][0]['reason'],'ready_file_missing')
            row['status']='expired'
            self.assertFalse(recovery.check_files([row],d)['problems'])

    def test_traversal_and_noncanonical_storage_keys_fail(self):
        with tempfile.TemporaryDirectory() as d:
            for key in ('../escape', str(uuid4()).upper(), '/absolute'):
                row=dict(id='test',storage_key=key,status='ready')
                self.assertEqual(recovery.check_files([row],d)['problems'][0]['reason'],'storage_key_not_canonical')


if __name__=='__main__':unittest.main()
