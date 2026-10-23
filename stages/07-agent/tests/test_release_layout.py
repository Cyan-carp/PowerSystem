"""Short offline tests for the host-only release layout contract."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('release_worker',Path(__file__).parents[1]/'scripts/interpretation-release-worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
TEST_TEMP=Path(__file__).parents[3]/'artifacts'


class PrivateLayoutTest(unittest.TestCase):
    def test_keeps_actual_executor_layout_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as temp:
            root=Path(temp);private=root/'original/private';executor=private/'executor'
            executor.mkdir(parents=True);(executor/'registry.json').write_text('{}')
            original=private/'original.env';original.write_text('SYNTHETIC=1')
            row={'Mounts':[{'Type':'bind','Source':str(executor),'Destination':'/run/ma-executor'}]}
            target=worker.private_environment_path(original,row,'candidate-r4')
            self.assertEqual(target,private/'candidate-r4.env')
            self.assertEqual(original.read_text(),'SYNTHETIC=1')
            target.write_text('SYNTHETIC=2')
            with self.assertRaisesRegex(AssertionError,'environment_exists'):
                worker.private_environment_path(original,row,'candidate-r4')

    def test_new_batch_environment_cannot_redirect_registry(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as temp:
            root=Path(temp);original=root/'new/private/integrated.env'
            original.parent.mkdir(parents=True);original.write_text('SYNTHETIC=1')
            row={'Mounts':[{'Type':'bind','Source':str(root/'old/private/executor'),'Destination':'/run/ma-executor'}]}
            with self.assertRaisesRegex(AssertionError,'missing_executor'):
                worker.private_environment_path(original,row,'candidate-r4')
            (original.parent/'executor').mkdir();(original.parent/'executor/registry.json').write_text('{}')
            with self.assertRaisesRegex(AssertionError,'mount_mismatch'):
                worker.private_environment_path(original,row,'candidate-r4')


if __name__=='__main__':unittest.main()
