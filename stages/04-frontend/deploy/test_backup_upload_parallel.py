"""Dual-copy completion, timing and owned-child cleanup without any network."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('backup_upload',Path(__file__).with_name('backup-upload.py'))
u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)

class Child:
    def __init__(self,pid,code,launches):self.pid=pid;self.code=code;self.launches=launches;self.calls=0;self.waited=False
    def poll(self):
        self.calls+=1
        if len(self.launches)<2:raise AssertionError('both copies must launch before waiting')
        return self.code if self.calls>1 else None
    def wait(self,timeout=None):self.waited=True;return self.code

class ParallelUploadTests(unittest.TestCase):
    def test_existing_timing_sidecar_handles_overlap_without_a_parent_span(self):
        spec=importlib.util.spec_from_file_location('parallel_timing',Path(__file__).with_name('backup-timing.py'))
        timing=importlib.util.module_from_spec(spec);spec.loader.exec_module(timing)
        rows=[{'name':'upload_primary','microseconds':10000000,'offset_microseconds':5000000,'bytes':1000},
              {'name':'upload_secondary','microseconds':20000000,'offset_microseconds':5000000,'bytes':1000}]
        result=timing.summarise(rows)
        self.assertEqual(result['total_seconds'],25)
        self.assertEqual({v['name'] for v in result['uploads']},{'upload_primary','upload_secondary'})

    def fixture(self,root):
        archive=Path(root)/'full.tar.gz.gpg';archive.write_bytes(b'encrypted-fixture')
        Path(str(archive)+'.sha256').write_text('fixture checksum')
        key=Path(root)/'identity-reference';key.touch()
        return archive,key,Path(root)/'stages.jsonl'

    def test_both_copies_launch_before_wait_and_record_individual_overlapping_spans(self):
        for failed in (False,True):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as directory:
                archive,key,stages=self.fixture(directory);launched=[]
                def spawn(args,**kw):
                    self.assertTrue(kw['start_new_session']);self.assertIn('StrictHostKeyChecking=yes',args)
                    child=Child(100+len(launched),1 if failed and not launched else 0,launched);launched.append(child);return child
                with patch.object(u.subprocess,'Popen',side_effect=spawn),patch.object(u.signal,'signal'),patch.object(u.time,'sleep'),patch.object(u.os,'killpg',create=True) as kill:
                    result=u.upload_copies(archive,key,{'upload_primary':'primary:/backup','upload_secondary':'secondary:/backup'},stages,time.time_ns())
                self.assertEqual(result,1 if failed else 0);self.assertTrue(all(c.waited for c in launched));kill.assert_not_called()
                rows=[json.loads(line) for line in stages.read_text().splitlines()]
                self.assertEqual({v['name'] for v in rows},{'upload_primary','upload_secondary'})
                self.assertEqual(len(rows),2);self.assertTrue(all(v['bytes']==archive.stat().st_size for v in rows))

    def test_interruption_terminates_only_owned_groups_and_reaps_both(self):
        with tempfile.TemporaryDirectory() as directory:
            archive,key,stages=self.fixture(directory);children=[]
            class Pending:
                def __init__(self,pid):self.pid=pid;self.waited=False
                def poll(self):return None
                def wait(self,timeout=None):self.waited=True;return -15
            def spawn(*args,**kw):
                child=Pending(200+len(children));children.append(child);return child
            with patch.object(u.subprocess,'Popen',side_effect=spawn),patch.object(u.signal,'signal'),patch.object(u.time,'sleep',side_effect=InterruptedError),patch.object(u.os,'killpg',create=True) as kill:
                self.assertEqual(u.upload_copies(archive,key,{'upload_primary':'p:/b','upload_secondary':'s:/b'},stages,time.time_ns()),130)
                self.assertEqual({v.args[0] for v in kill.call_args_list},{200,201});self.assertTrue(all(c.waited for c in children))

    def test_identical_targets_rejected_before_any_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            archive,key,stages=self.fixture(directory)
            with patch.object(u.subprocess,'Popen') as spawn,self.assertRaises(ValueError):
                u.upload_copies(archive,key,{'upload_primary':'p:/b','upload_secondary':'p:/b'},stages,time.time_ns())
            spawn.assert_not_called()

    def test_signal_during_spawn_registers_child_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            archive,key,stages=self.fixture(directory);handlers={};children=[]
            class Pending:
                pid=300
                def poll(self):return None
                def wait(self,timeout=None):self.waited=True;return -15
            def handler(name,value):handlers[name]=value
            def spawn(*args,**kw):
                handlers[u.signal.SIGTERM](u.signal.SIGTERM,None)
                child=Pending();children.append(child);return child
            with patch.object(u.subprocess,'Popen',side_effect=spawn),patch.object(u.signal,'signal',side_effect=handler),patch.object(u.os,'killpg',create=True) as kill:
                self.assertEqual(u.upload_copies(archive,key,{'upload_primary':'p:/b','upload_secondary':'s:/b'},stages,time.time_ns()),130)
                kill.assert_called_once_with(300,u.signal.SIGTERM)
                self.assertEqual(len(children),1);self.assertTrue(children[0].waited)

if __name__=='__main__':unittest.main()
