#!/usr/bin/env python3
"""Explicit machine-bound PowerSystem host registration; never a hostname bypass."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

PROFILE = Path('/etc/powersystem/host-profiles.json')
PROJECT = 'powersystem-stage4'
CAPABILITIES = {'release', 'backup', 'restore', 'registry', 'observe', 'migration'}


class HostError(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise HostError(reason)


def identity():
    virt = subprocess.run(['systemd-detect-virt'], capture_output=True, timeout=10)
    return {'hostname': os.uname().nodename,
            'machine_id_sha256': hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest(),
            'dmi_uuid': Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower(),
            'virtualization': virt.stdout.decode().strip() or 'none'}


def select_profile(document, actual, capability):
    require(capability in CAPABILITIES, 'host_capability_unknown')
    require(document.get('format') == 1 and document.get('project') == PROJECT,
            'host_registry_invalid')
    matches = [host for host in document.get('hosts', [])
               if all(host.get(key) == actual.get(key) for key in
                      ('hostname', 'machine_id_sha256', 'dmi_uuid', 'virtualization'))]
    require(len(matches) == 1, 'registered_machine_identity_required')
    host = matches[0]
    require(host.get('enabled') is True and capability in host.get('capabilities', []),
            'host_capability_not_registered')
    require(host.get('profile') == 'powersystem-r0' and
            host.get('project_root') == '/opt/powersystem', 'host_project_scope_invalid')
    return host


def require_registered_host(capability):
    require(os.geteuid() == 0, 'registered_root_host_required')
    require(PROFILE.is_file() and not PROFILE.is_symlink(), 'host_registry_missing')
    metadata = PROFILE.stat()
    require(metadata.st_uid == 0 and not stat.S_IMODE(metadata.st_mode) & 0o077,
            'host_registry_permissions_invalid')
    host = select_profile(json.loads(PROFILE.read_text()), identity(), capability)
    fence = Path('/opt/powersystem/runtime/migration-source-fenced.json')
    if capability in {'release', 'backup', 'registry', 'observe'} and fence.exists():
        marker = json.loads(fence.read_text())
        require(marker.get('source_host_id') != host['host_id'], 'migration_source_is_fenced')
    return host


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capability', choices=sorted(CAPABILITIES))
    args = parser.parse_args()
    try:
        host = require_registered_host(args.capability)
        print(json.dumps({'registered': True, 'host_id': host['host_id'],
                          'hostname': host['hostname'], 'capability': args.capability}))
    except (HostError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'registered': False, 'blocked': str(error)}))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
