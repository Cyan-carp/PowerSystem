"""Fail-closed migration receipts; no Docker, credentials or network."""
import copy
import importlib.util
from pathlib import Path
import re
import types
import unittest

spec=importlib.util.spec_from_file_location('migration_vm_test',Path(__file__).with_name('migration-vm.py'))
mv=importlib.util.module_from_spec(spec);spec.loader.exec_module(mv)

class MigrationReceiptTests(unittest.TestCase):
    def setUp(self):
        def require(value,reason):
            if not value:raise ValueError(reason)
        self.ps=types.SimpleNamespace(require=require,PROJECT='powersystem-stage4',ID=re.compile(r'sha256:[a-f0-9]{64}'))
        self.host={'host_id':'powersystem-prod-vm117'}
        self.receipt={'format':1,'project':'powersystem-stage4','target_host_id':self.host['host_id'],
            'source_host_id':'r730xd-source','source_fenced':True,'source_services_stopped':True,
            'volumes':{str(i):{'verified':True,'source_sha256':'a'*64,'target_sha256':'a'*64} for i in range(13)},
            'mutable_bindings_verified':True,'role_files_verified':True,'source_backup':{'dual_copies_verified':True},
            'images':{str(i):'sha256:'+'a'*64 for i in range(20)},'no_replay_pending':True}
    def check(self,receipt):mv.require_receipt_scope(self.ps,receipt,self.host)
    def test_complete_scope_accepted(self):self.check(self.receipt)
    def test_wrong_host_scope_or_source_rejected(self):
        for k,v in [('target_host_id','other'),('source_host_id','other'),('project','other'),('format',2)]:
            with self.subTest(k=k),self.assertRaises(ValueError):self.check({**self.receipt,k:v})
    def test_source_must_be_stopped_and_fenced(self):
        for k in ('source_fenced','source_services_stopped'):
            with self.subTest(k=k),self.assertRaises(ValueError):self.check({**self.receipt,k:False})
    def test_all_thirteen_volumes_must_match(self):
        r=copy.deepcopy(self.receipt);r['volumes'].pop('0')
        with self.assertRaises(ValueError):self.check(r)
        for k,v in [('verified',False),('target_sha256','b'*64),('source_sha256','')]:
            r=copy.deepcopy(self.receipt);r['volumes']['0'][k]=v
            with self.subTest(k=k),self.assertRaises(ValueError):self.check(r)
    def test_private_files_and_dual_backup_required(self):
        for k in ('mutable_bindings_verified','role_files_verified'):
            with self.subTest(k=k),self.assertRaises(ValueError):self.check({**self.receipt,k:False})
        with self.assertRaises(ValueError):self.check({**self.receipt,'source_backup':{}})
    def test_pending_work_must_not_replay(self):
        with self.assertRaisesRegex(ValueError,'pending_work'):self.check({**self.receipt,'no_replay_pending':False})
    def test_all_images_must_be_exact_ids(self):
        r=copy.deepcopy(self.receipt);r['images']['0']='image:latest'
        with self.assertRaises(ValueError):self.check(r)
    def test_private_remap_preserves_protocol_keys_and_unrelated_paths(self):
        original={'bind':'/old/private/executor','other':'/other/private/file',
                  'protocol':'ma/v1-bound-read','env':['/old/private/rag.env','/old/private/rag-key']}
        result=mv.remap_private(original,'/old/private','/new/private')
        self.assertEqual(result['bind'],'/new/private/executor')
        self.assertEqual(result['env'],['/new/private/rag.env','/new/private/rag-key'])
        self.assertEqual(result['other'],original['other'])
        self.assertEqual(result['protocol'],original['protocol'])

if __name__=='__main__':unittest.main()
