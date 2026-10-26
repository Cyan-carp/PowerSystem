"""Offline temp-tree tests for verify-host-recovery.py (no network, no host)."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('verify_host_recovery', HERE / 'verify-host-recovery.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

REQUIRED = (list(verify.HOST_CONTROL_TOOLS) + list(verify.HOST_SINGLE_FILES)
            + list(verify.COLLECTOR_REGISTRY) + [verify.COLLECTOR_CODE_ROOT + '/collector.py',
                                                 verify.CLOUD_ROUTES] + list(verify.WEB_FRP_UNITS)
            + [verify.WEB_FRP_CONFIG + '/primary.toml', verify.WEB_FRP_CONFIG + '/secondary.toml',
               '/etc/powersystem-edge/frp-token', '/etc/powersystem-edge/frp-server.crt',
               verify.WEB_FRP_BINARIES + '/frpc-primary', verify.WEB_FRP_BINARIES + '/frpc-secondary'])


def write_asset(ma, logical, data=None):
    data = data if data is not None else ('material:' + logical).encode()
    archive = Path('host-runtime', *Path(logical).parts[1:])
    target = ma / archive
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {'source': logical, 'logical_path': logical, 'archive': archive.as_posix(),
            'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'mode': 0o600}


def build_ma(root, omit=(), extra=()):
    ma = root / 'ma'
    ma.mkdir()
    configs = {
        verify.WEB_FRP_CONFIG + '/primary.toml':
            b'auth.tokenSource.file.path="/etc/powersystem-edge/frp-token"\n[transport.tls]\ntrustedCaFile="/etc/powersystem-edge/frp-server.crt"\n',
        verify.WEB_FRP_CONFIG + '/secondary.toml': b'serverAddr="fixture"\n',
    }
    assets = [write_asset(ma, logical, configs.get(logical)) for logical in REQUIRED if logical not in omit]
    assets += [write_asset(ma, logical) for logical in extra]
    manifest = {'version': 'ma-backup/v1', 'host_runtime': {
        'restore_material_only': True, 'restore_activation_forbidden': True,
        'host_registration_evidence_only': True, 'credentials_encrypted_archive_only': True,
        'assets': assets,
        'symlinks': [{'source': '/opt/powersystem-ops-read/current',
                      'logical_path': '/opt/powersystem-ops-read/current',
                      'target': 'collector.py'}],
        'web_frp': {'configs': [verify.WEB_FRP_CONFIG + '/primary.toml',
                                verify.WEB_FRP_CONFIG + '/secondary.toml'],
                    'references': {'auth.tokenSource.file.path': '/etc/powersystem-edge/frp-token',
                                   'transport.tls.trustedCaFile': '/etc/powersystem-edge/frp-server.crt'},
                    'activation_forbidden': True},
        'counts': {'files': len(assets), 'bytes': 0, 'symlinks': 1}}}
    (ma / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return ma, manifest


class HostRecoveryTests(unittest.TestCase):
    def test_valid_material_passes_and_reports_counts_only(self):
        with tempfile.TemporaryDirectory() as d:
            ma, manifest = build_ma(Path(d))
            result = verify.verify_host_runtime(ma, require_vm=True)
            self.assertEqual(result['status'], 'pass', result['errors'])
            self.assertEqual(result['files'], len(manifest['host_runtime']['assets']))
            self.assertGreater(result['bytes'], 0)
            self.assertEqual(result['symlinks'], 1)
            self.assertTrue(result['activation_forbidden'])
            self.assertEqual(result['errors'], [])

    def test_tampered_byte_fails_hash(self):
        with tempfile.TemporaryDirectory() as d:
            ma, _ = build_ma(Path(d))
            target = ma / 'host-runtime' / 'etc/sudoers.d/powersystem-ops-read'
            target.write_bytes(target.read_bytes() + b'tamper')
            result = verify.verify_host_runtime(ma)
            self.assertEqual(result['status'], 'fail')
            self.assertIn('archive_bytes_mismatch', result['errors'])

    def test_archive_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ma, manifest = build_ma(Path(d))
            manifest['host_runtime']['assets'][0]['archive'] = '../../escape'
            (ma / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            result = verify.verify_host_runtime(ma)
            self.assertEqual(result['status'], 'fail')
            self.assertIn('archive_traversal_forbidden', result['errors'])

    def test_declared_symlink_forbidden_path_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ma, manifest = build_ma(Path(d))
            manifest['host_runtime']['symlinks'][0]['logical_path'] = '/root/.ssh/id_ed25519'
            (ma / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            result = verify.verify_host_runtime(ma)
            self.assertEqual(result['status'], 'fail')
            self.assertIn('forbidden_recovery_material', result['errors'])

    def test_symlink_inside_archive_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ma, _ = build_ma(Path(d))
            outside = Path(d) / 'outside'
            outside.mkdir()
            (outside / 'secret').write_text('x')
            target = ma / 'host-runtime' / 'etc/sudoers.d/powersystem-ops-read'
            target.unlink()
            try:
                os.symlink(outside / 'secret', target)
            except OSError:
                self.skipTest('symlink creation not permitted on this platform')
            result = verify.verify_host_runtime(ma)
            self.assertEqual(result['status'], 'fail')
            self.assertIn('archive_symlink_forbidden', result['errors'])

    def test_forbidden_material_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ma, _ = build_ma(Path(d), extra=['/etc/pve/qemu-server/117.conf'])
            result = verify.verify_host_runtime(ma)
            self.assertEqual(result['status'], 'fail')
            self.assertIn('forbidden_recovery_material', result['errors'])

    def test_require_vm_missing_units_rejected_but_legacy_skips(self):
        with tempfile.TemporaryDirectory() as d:
            ma, _ = build_ma(Path(d), omit=[verify.CLOUD_ROUTES, verify.WEB_FRP_UNITS[0]])
            self.assertEqual(verify.verify_host_runtime(ma)['status'], 'pass')
            result = verify.verify_host_runtime(ma, require_vm=True)
            self.assertEqual(result['status'], 'fail')
            self.assertTrue(result['errors'][0].startswith('require_vm_missing'))

    def test_legacy_manifest_without_host_runtime_skips(self):
        with tempfile.TemporaryDirectory() as d:
            ma = Path(d) / 'ma'
            ma.mkdir()
            (ma / 'manifest.json').write_text(json.dumps({'version': 'ma-backup/v1'}), encoding='utf-8')
            result = verify.verify_host_runtime(ma)
            self.assertEqual(result['status'], 'skip')
            self.assertEqual(verify.verify_host_runtime(ma, require_vm=True)['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
