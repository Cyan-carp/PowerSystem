"""Host recovery material: exact whitelist, explicit FRP refs, no identity."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'backup_ma_host_assets', Path(__file__).parents[2] / '09-ops-agent/deploy/backup-ma.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)

CONTROL_TOOLS = (
    '/opt/powersystem/stages/04-frontend/deploy/production-stack.py',
    '/opt/powersystem/stages/04-frontend/deploy/host-profile.py',
    '/opt/powersystem/stages/04-frontend/deploy/migration-vm.py',
    '/opt/powersystem/stages/04-frontend/deploy/migration-data.py',
    '/opt/powersystem/stages/04-frontend/deploy/source-build.py',
    '/opt/powersystem/stages/09-ops-agent/deploy/backup-ma.py',
    '/opt/powersystem/stages/09-ops-agent/deploy/restore-ma.py',
    '/opt/powersystem/stages/09-ops-agent/deploy/ma-release.py',
    '/opt/powersystem/stages/09-ops-agent/deploy/observe-ma.py',
)
SINGLE_FILES = (
    '/usr/local/libexec/powersystem-ops-read',
    '/usr/local/libexec/powersystem-ops-ssh-entry',
    '/etc/sudoers.d/powersystem-ops-read',
    '/var/lib/powersystem-ops-read/.ssh/authorized_keys',
    '/etc/systemd/system/powersystem-backup.service',
    '/etc/systemd/system/powersystem-backup.timer',
)


class HostAssetCaptureTests(unittest.TestCase):
    def fixture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)

        def write(logical, data=None):
            path = root / str(logical).lstrip('/')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if data is not None else ('fixture:' + str(logical) + '\n').encode())
            return path

        for logical in CONTROL_TOOLS + SINGLE_FILES:
            write(logical)
        for logical in ('/etc/powersystem-ops-read/registry.json',
                        '/etc/powersystem-ops-read/installation.json',
                        '/etc/powersystem-ops-read/db-password',
                        '/opt/powersystem-ops-read/code/collector/broker.py',
                        '/opt/powersystem-ops-read/venv/pyvenv.cfg',
                        '/opt/powersystem-ops-read/venv/bin/python'):
            write(logical)
        return root, write

    def capture(self, root):
        ma = root / 'archive/ma'
        ma.mkdir(parents=True)
        return ma, backup.capture_host_assets(ma, root=root)

    def test_whitelisted_role_files_are_archived_with_matching_digests(self):
        root, _ = self.fixture()
        ma, result = self.capture(root)
        logical = {item['logical_path'] for item in result['assets']}
        for expected in CONTROL_TOOLS + SINGLE_FILES + (
                '/etc/powersystem-ops-read/registry.json',
                '/etc/powersystem-ops-read/db-password',
                '/opt/powersystem-ops-read/code/collector/broker.py',
                '/opt/powersystem-ops-read/venv/bin/python'):
            self.assertIn(expected, logical)
        self.assertIsNone(result['web_frp'])
        self.assertTrue(result['restore_material_only'])
        self.assertTrue(result['restore_activation_forbidden'])
        self.assertTrue(result['credentials_encrypted_archive_only'])
        self.assertEqual(result['counts']['files'], len(result['assets']))
        for item in result['assets']:
            source = root / item['logical_path'].lstrip('/')
            archived = ma / item['archive']
            self.assertEqual(item['archive'], 'host-runtime/' + item['logical_path'].lstrip('/'))
            self.assertEqual(item['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(item['bytes'], len(source.read_bytes()))
            self.assertEqual(archived.read_bytes(), source.read_bytes())

    def test_web_frp_capture_is_limited_to_explicit_references(self):
        root, write = self.fixture()
        write('/etc/powersystem-edge/frp-token', b'fixture-token\n')
        write('/etc/powersystem-edge/frp-server.crt', b'fixture-ca\n')
        write('/etc/powersystem-edge/frpc.toml', b'shared-source-config\n')
        write('/opt/frp/frpc', b'shared-source-binary\n')
        write('/opt/powersystem-web-frp/frpc-primary', b'web-primary\n')
        write('/opt/powersystem-web-frp/frpc-secondary', b'web-secondary\n')
        for unit in ('powersystem-web-primary-frpc.service', 'powersystem-web-secondary-frpc.service'):
            write('/etc/systemd/system/' + unit)
        write('/etc/powersystem-web-frp/frpc-primary.toml', (
            'serverAddr = "8.138.42.212"\n'
            'auth.method = "token"\n'
            'auth.tokenSource.type = "file"\n'
            'auth.tokenSource.file.path = "/etc/powersystem-edge/frp-token"\n'
            '[transport.tls]\n'
            'certFile = "/etc/powersystem-web-frp/client.crt"\n').encode())
        write('/etc/powersystem-web-frp/client.crt', b'client-cert\n')
        write('/etc/powersystem-web-frp/frpc-secondary.toml', (
            '[transport.tls]\n'
            'trustedCaFile = "/etc/powersystem-edge/frp-server.crt"\n').encode())
        ma, result = self.capture(root)
        frp = result['web_frp']
        self.assertEqual(sorted(frp['references']), ['auth.tokenSource.file.path',
                                                     'transport.tls.certFile',
                                                     'transport.tls.trustedCaFile'])
        self.assertEqual(frp['references']['auth.tokenSource.file.path'],
                         '/etc/powersystem-edge/frp-token')
        self.assertTrue(frp['activation_forbidden'])
        logical = {item['logical_path'] for item in result['assets']}
        for expected in ('/etc/powersystem-edge/frp-token',
                         '/etc/powersystem-web-frp/client.crt',
                         '/opt/powersystem-web-frp/frpc-primary',
                         '/opt/powersystem-web-frp/frpc-secondary',
                         '/etc/systemd/system/powersystem-web-primary-frpc.service',
                         '/etc/systemd/system/powersystem-web-secondary-frpc.service'):
            self.assertIn(expected, logical)
        self.assertNotIn('/etc/powersystem-edge/frpc.toml', logical)
        self.assertNotIn('/opt/frp/frpc', logical)
        self.assertFalse(any(path.startswith('host-runtime/opt/frp') for path in logical))
        self.assertTrue((ma / 'host-runtime/etc/powersystem-edge/frp-token').is_file())

    def test_frp_reference_outside_owned_scope_is_rejected(self):
        root, write = self.fixture()
        write('/opt/powersystem-web-frp/frpc-primary', b'web-primary\n')
        write('/opt/powersystem-web-frp/frpc-secondary', b'web-secondary\n')
        for unit in ('powersystem-web-primary-frpc.service', 'powersystem-web-secondary-frpc.service'):
            write('/etc/systemd/system/' + unit)
        write('/etc/powersystem-web-frp/frpc-primary.toml', (
            'auth.tokenSource.type = "file"\n'
            'auth.tokenSource.file.path = "/etc/nginx/frp-token"\n').encode())
        write('/etc/nginx/frp-token', b'outside\n')
        ma = root / 'archive/ma'
        ma.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'outside_owned_scope'):
            backup.capture_host_assets(ma, root=root)

    def test_machine_identity_and_backup_passphrase_are_never_archived(self):
        root, write = self.fixture()
        for logical in ('/etc/machine-id', '/root/.ssh/id_ed25519', '/root/.ssh/authorized_keys',
                        '/etc/powersystem/backup-passphrase', '/etc/pve/qemu-server/117.conf'):
            write(logical)
            self.assertTrue(backup.host_path_forbidden(logical), logical)
        self.assertFalse(backup.host_path_forbidden('/var/lib/powersystem-ops-read/.ssh/authorized_keys'))
        ma, result = self.capture(root)
        logical = {item['logical_path'] for item in result['assets']}
        for forbidden in ('/etc/machine-id', '/root/.ssh/id_ed25519', '/root/.ssh/authorized_keys',
                          '/etc/powersystem/backup-passphrase', '/etc/pve/qemu-server/117.conf'):
            self.assertNotIn(forbidden, logical)
        archived = [str(path) for path in (ma / 'host-runtime').rglob('*')]
        self.assertFalse(any('machine-id' in path or 'passphrase' in path or '/root/.ssh' in path
                             or '/etc/pve' in path for path in archived))
        manifest = json.dumps(result)
        self.assertNotIn('backup-passphrase', manifest)
        self.assertNotIn('/root/.ssh', manifest)


if __name__ == '__main__':
    unittest.main()
