"""Actual RAG references remain captured when Ops retains its old env label."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

def module(name, filename):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(filename))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value

backup=module('backup_ma_rag_config','backup-ma.py')
restore=module('restore_ma_rag_config','restore-ma.py')

class ActualRagBackupTests(unittest.TestCase):
    def test_actual_new_key_roundtrip_and_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();private=root/'runtime/ma-m4-fixture/private'
            supervisor=private/'supervisor-r0';supervisor.mkdir(parents=True)
            (private/'old-ops.env').write_text('MA_SUPERVISOR_PRIVATE_DIR=old-supervisor\n')
            key=supervisor/'rag-official-key';key.write_bytes(b'synthetic-new-assistant-key')
            (supervisor/'actor-key').write_bytes(b'synthetic-unchanged-actor')
            rag=private/'r0-rag.env';rag.write_text('V4_RUNS_ENABLED=true\n')
            origin=private/'r0.env';origin.write_text('RAG_ENV_FILE='+str(rag)+'\nMA_RAG_ENV_FILE='+str(rag)+'\n')
            info={'Config':{'Labels':{'com.docker.compose.project':'powersystem-stage4','com.docker.compose.service':'agent-rag','com.docker.compose.project.environment_file':str(origin)}},
                  'Mounts':[{'Destination':'/run/ma-supervisor','Type':'bind','RW':False,'Source':str(supervisor)},
                            {'Destination':'/run/secrets/rag_llm_key','Type':'bind','RW':False,'Source':str(key)}]}
            target=root/'archive/ma';target.mkdir(parents=True)
            with patch.object(backup,'ROOT',root):
                capture=backup.capture_rag_configuration(target,info)
            self.assertEqual(len(capture['files']),4)
            self.assertEqual(restore.verify_rag_configuration(target,{'actual_rag_configuration':capture}),4)
            self.assertEqual(capture['origin_environment'],str(origin))
            keyname=next(k for k,v in capture['files'].items() if v['source']==str(key))
            (target/keyname).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'hash_mismatch'):
                restore.verify_rag_configuration(target,{'actual_rag_configuration':capture})
            info['Mounts'][0]['RW']=True
            with patch.object(backup,'ROOT',root), self.assertRaisesRegex(ValueError,'readonly_rag_private_bind'):
                backup.capture_rag_configuration(target,info)

    def test_old_archives_and_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.assertEqual(restore.verify_rag_configuration(root,{}),0)
            outside=root/'outside';outside.write_bytes(b'fixture')
            entry={'restore_execution_forbidden':True,'files':{'outside':{'sha256':hashlib.sha256(b'fixture').hexdigest()}}}
            with self.assertRaisesRegex(ValueError,'path_invalid'):
                restore.verify_rag_configuration(root,{'actual_rag_configuration':entry})

if __name__=='__main__':unittest.main()
