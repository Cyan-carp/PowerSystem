"""Offline verification of ma/host-runtime recovery material.

Read-only: opens the archive tree, never creates symlinks, never activates
anything, never touches the network or a host.  A PASS here only proves the
captured bytes match the manifest; it is not evidence that a real VM restore
succeeded.
"""
import argparse
import hashlib
import json
import os
import tomllib
from pathlib import Path, PurePosixPath

HOST_CONTROL_TOOLS = (
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
HOST_SINGLE_FILES = (
    '/usr/local/libexec/powersystem-ops-read',
    '/usr/local/libexec/powersystem-ops-ssh-entry',
    '/etc/sudoers.d/powersystem-ops-read',
    '/var/lib/powersystem-ops-read/.ssh/authorized_keys',
    '/etc/systemd/system/powersystem-backup.service',
    '/etc/systemd/system/powersystem-backup.timer',
)
COLLECTOR_REGISTRY = ('/etc/powersystem-ops-read/registry.json',
                      '/etc/powersystem-ops-read/installation.json')
COLLECTOR_CODE_ROOT = '/opt/powersystem-ops-read'
WEB_FRP_CONFIG = '/etc/powersystem-web-frp'
WEB_FRP_BINARIES = '/opt/powersystem-web-frp'
WEB_FRP_UNITS = ('/etc/systemd/system/powersystem-web-primary-frpc.service',
                 '/etc/systemd/system/powersystem-web-secondary-frpc.service')
CLOUD_ROUTES = '/etc/systemd/network/10-netplan-management.network.d/50-powersystem-cloud-routes.conf'
FORBIDDEN_ROOTS = ('/etc/pve', '/root/.ssh')
FORBIDDEN_NAMES = ('machine-id', 'backup-passphrase')
FIXED_FLAGS = ('restore_material_only', 'restore_activation_forbidden',
               'host_registration_evidence_only', 'credentials_encrypted_archive_only')


def _logical(value):
    if isinstance(value, PurePosixPath):
        path = value
    elif isinstance(value, str) and value.startswith('/'):
        path = PurePosixPath(value)
    else:
        raise ValueError('logical_path_absolute_required')
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('logical_path_traversal_forbidden')
    return path


def _archive(value):
    if not isinstance(value, str) or value.startswith('/'):
        raise ValueError('archive_relative_required')
    path = PurePosixPath(value)
    if '..' in path.parts or '.' in path.parts or not path.parts:
        raise ValueError('archive_traversal_forbidden')
    return path


def _forbidden(logical):
    path = _logical(logical)
    if any(path.is_relative_to(PurePosixPath(root)) for root in FORBIDDEN_ROOTS):
        return True
    name = path.name.lower()
    if name in FORBIDDEN_NAMES or 'passphrase' in name:
        return True
    return name.startswith('id_') and not name.endswith('.pub')


def _no_symlink_components(ma, relative):
    current = ma
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('archive_symlink_forbidden')
    resolved = (ma / relative).resolve()
    if not resolved.is_relative_to(ma.resolve()):
        raise ValueError('archive_escape_forbidden')
    return resolved


def _verify_asset(ma, item):
    if not isinstance(item, dict):
        raise ValueError('asset_entry_invalid')
    logical = _logical(item.get('logical_path'))
    source = _logical(item.get('source'))
    archive = _archive(item.get('archive'))
    if _forbidden(logical) or _forbidden(source):
        raise ValueError('forbidden_recovery_material')
    expected = PurePosixPath('host-runtime', *logical.parts[1:])
    if archive != expected:
        raise ValueError('archive_path_not_canonical')
    target = _no_symlink_components(ma, Path(*archive.parts))
    if not target.is_file():
        raise ValueError('archive_file_missing')
    data = target.read_bytes()
    if item.get('bytes') != len(data):
        raise ValueError('archive_bytes_mismatch')
    if item.get('sha256') != hashlib.sha256(data).hexdigest():
        raise ValueError('archive_sha256_mismatch')
    return str(logical), len(data)


def _verify_symlinks(symlinks):
    count = 0
    for item in symlinks or ():
        if not isinstance(item, dict):
            raise ValueError('symlink_entry_invalid')
        logical = _logical(item.get('logical_path'))
        _logical(item.get('source'))
        if _forbidden(logical) or _forbidden(item.get('source')):
            raise ValueError('forbidden_recovery_material')
        if not isinstance(item.get('target'), str) or not item['target']:
            raise ValueError('symlink_target_missing')
        count += 1
    return count


def _verify_web_frp(ma, web_frp, logicals, require_vm=False):
    if not isinstance(web_frp, dict) or web_frp.get('activation_forbidden') is not True:
        raise ValueError('web_frp_activation_not_forbidden')
    configs = [_logical(item) for item in web_frp.get('configs') or ()]
    if not configs:
        raise ValueError('web_frp_config_missing')
    if any(str(item) not in logicals for item in configs):
        raise ValueError('web_frp_config_not_archived')
    references = web_frp.get('references') or {}
    if not isinstance(references, dict) or (require_vm and not references):
        raise ValueError('web_frp_references_missing')
    for key, value in references.items():
        if not isinstance(key, str) or not key:
            raise ValueError('web_frp_reference_invalid')
        if str(_logical(value)) not in logicals:
            raise ValueError('web_frp_reference_not_archived')
    binaries = [item for item in logicals if PurePosixPath(item).is_relative_to(PurePosixPath(WEB_FRP_BINARIES))]
    if len(binaries) < 2:
        raise ValueError('web_frp_binaries_missing')
    # Credentials are referenced by exact TOML fields; file naming conveys no
    # authority (the deployed token is /etc/powersystem-edge/frp-token).
    actual_references = {}
    for config in configs:
        if not config.is_relative_to(PurePosixPath(WEB_FRP_CONFIG)):
            raise ValueError('web_frp_config_outside_scope')
        path = _no_symlink_components(ma, Path('host-runtime', *config.parts[1:]))
        try:
            document = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError:
            raise ValueError('web_frp_config_invalid') from None
        token = document.get('auth', {}).get('tokenSource', {}).get('file', {}).get('path')
        explicit = [('auth.tokenSource.file.path', token)]
        tls = document.get('transport', {}).get('tls', {})
        explicit += [('transport.tls.' + key, tls.get(key))
                     for key in ('certFile', 'keyFile', 'trustedCaFile')]
        for key, value in explicit:
            if not value:
                continue
            logical = _logical(value)
            if not any(logical.is_relative_to(PurePosixPath(root)) for root in
                       ('/etc/powersystem-edge', WEB_FRP_CONFIG)) or _forbidden(logical):
                raise ValueError('web_frp_reference_outside_scope')
            if str(logical) not in logicals:
                raise ValueError('web_frp_reference_not_archived')
            actual_references[key] = str(logical)
    if actual_references != references:
        raise ValueError('web_frp_declared_references_mismatch')


def _require_vm(logicals):
    missing = [item for item in HOST_CONTROL_TOOLS + HOST_SINGLE_FILES + WEB_FRP_UNITS
               + (CLOUD_ROUTES,) + COLLECTOR_REGISTRY if item not in logicals]
    if missing:
        raise ValueError('require_vm_missing:' + missing[0])
    if not any(PurePosixPath(item).is_relative_to(PurePosixPath(COLLECTOR_CODE_ROOT)) for item in logicals):
        raise ValueError('require_vm_missing:collector_code')
    for unit in WEB_FRP_UNITS:
        if unit not in logicals:
            raise ValueError('require_vm_missing:' + unit)


def verify_host_runtime(ma, require_vm=False):
    """Verify only declared host_runtime material; never activates anything."""
    ma = Path(ma)
    manifest_path = ma / 'manifest.json'
    result = {'status': 'fail', 'files': 0, 'bytes': 0, 'symlinks': 0,
              'activation_forbidden': True, 'errors': []}
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        result['errors'].append('manifest_unreadable')
        return result
    host_runtime = manifest.get('host_runtime')
    if host_runtime is None:
        if require_vm:
            result['errors'].append('host_runtime_required')
        else:
            result['status'] = 'skip'
            result['reason'] = 'host_runtime_absent'
        return result
    try:
        if not isinstance(host_runtime, dict):
            raise ValueError('host_runtime_invalid')
        for flag in FIXED_FLAGS:
            if host_runtime.get(flag) is not True:
                raise ValueError('flag_not_true:' + flag)
        assets = host_runtime.get('assets')
        if not isinstance(assets, list) or not assets:
            raise ValueError('assets_missing')
        logicals = set()
        for item in assets:
            logical, size = _verify_asset(ma, item)
            if logical in logicals:
                raise ValueError('duplicate_asset')
            logicals.add(logical)
            result['files'] += 1
            result['bytes'] += size
        result['symlinks'] = _verify_symlinks(host_runtime.get('symlinks'))
        if require_vm and host_runtime.get('web_frp') is None:
            raise ValueError('web_frp_required')
        if host_runtime.get('web_frp') is not None:
            _verify_web_frp(ma, host_runtime['web_frp'], logicals, require_vm=require_vm)
        if require_vm:
            _require_vm(logicals)
        result['status'] = 'pass'
    except ValueError as error:
        result['errors'].append(str(error))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify host_runtime recovery material offline')
    parser.add_argument('--ma', type=Path, required=True)
    parser.add_argument('--require-vm', action='store_true')
    args = parser.parse_args(argv)
    result = verify_host_runtime(args.ma, require_vm=args.require_vm)
    print(json.dumps({'status': result['status'], 'files': result['files'],
                      'bytes': result['bytes'], 'symlinks': result['symlinks'],
                      'activation_forbidden': result['activation_forbidden'],
                      'errors': result['errors']}, ensure_ascii=False))
    return 0 if result['status'] in ('pass', 'skip') else 1


if __name__ == '__main__':
    raise SystemExit(main())
