import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ssh_admin_source', Path(__file__).parents[1] / 'refresh-ssh-admin-source.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SourceGuards(unittest.TestCase):
    def test_non_root_cannot_apply_or_execute_commands(self):
        with patch.object(module.os, 'geteuid', return_value=1000, create=True), patch.object(module, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'new_cloud_root_required'):
                module.main(True)
            command.assert_not_called()

    def test_other_host_cannot_apply(self):
        with patch.object(module.os, 'geteuid', return_value=0, create=True), patch.object(module, 'command', return_value='256 SHA256:other other ED25519'):
            with self.assertRaisesRegex(ValueError, 'new_cloud_identity_mismatch'):
                module.main(True)

    def test_forwarded_or_console_session_cannot_apply(self):
        pin='256 SHA256:PUS6mFBOfwdsITlg6Wxl0PP5FYg9wo/4pqrxADeJsyQ cloud ED25519'
        with patch.object(module.os, 'geteuid', return_value=0, create=True), patch.object(module, 'command', return_value=pin):
            for connection in ('', '203.0.113.5 50000 10.0.0.1 7070'):
                with self.subTest(connection=connection), patch.dict(module.os.environ, {'SSH_CONNECTION':connection}):
                    with self.assertRaisesRegex(ValueError, 'direct_cloud_ssh22_required'):
                        module.main(True)
            with patch.dict(module.os.environ, {'SSH_CONNECTION':'127.0.0.1 50000 10.0.0.1 22'}):
                with self.assertRaisesRegex(ValueError, 'public_ipv4_source_required'):
                    module.main(True)

    def test_live_counters_ignored_but_non_admin_policy_change_detected(self):
        before='*filter\n:INPUT ACCEPT [2:100]\n-A POWERSYSTEM_EDGE -p tcp --dport 7000 -j DROP\nCOMMIT\n'
        counter_only=before.replace('[2:100]', '[99:9900]')
        broadened=counter_only.replace('--dport 7000 -j DROP', '--dport 7000 -j ACCEPT')
        self.assertEqual(module.normalized(before), module.normalized(counter_only))
        self.assertNotEqual(module.normalized(before), module.normalized(broadened))


if __name__ == '__main__':
    unittest.main()
