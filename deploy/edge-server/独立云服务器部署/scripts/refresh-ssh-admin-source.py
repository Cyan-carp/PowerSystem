"""Run through authenticated direct SSH :22 on the verified new cloud only."""
import argparse
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=15, check=True).stdout


def normalized(rules):
    return [re.sub(r'\[\d+:\d+\]', '[COUNTERS]', line) for line in rules.splitlines()
            if line.startswith(('-A ', ':', '*', 'COMMIT'))]


def main(apply):
    if os.geteuid() != 0:
        raise ValueError('new_cloud_root_required')
    fingerprint = command(['ssh-keygen', '-lf', '/etc/ssh/ssh_host_ed25519_key.pub']).split()[1]
    if fingerprint != 'SHA256:PUS6mFBOfwdsITlg6Wxl0PP5FYg9wo/4pqrxADeJsyQ':
        raise ValueError('new_cloud_identity_mismatch')
    connection = os.environ.get('SSH_CONNECTION', '').split()
    if len(connection) != 4 or connection[3] != '22':
        raise ValueError('direct_cloud_ssh22_required')
    source = str(ipaddress.IPv4Address(connection[0]))
    if not ipaddress.IPv4Address(source).is_global:
        raise ValueError('public_ipv4_source_required')
    target = Path('/etc/powersystem-edge/allowed-admin-ipv4')
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('unsafe_admin_allowlist')
    previous = str(ipaddress.IPv4Address(target.read_text().strip()))
    if not apply or source == previous:
        print(json.dumps({'dry_run': not apply, 'source_matches': source == previous,
                          'changed': False, 'scope': 'new-cloud-7070-single-source'}))
        return
    before = command(['iptables-save'])
    allowed = [line for line in before.splitlines() if line.startswith('-A POWERSYSTEM_EDGE ')
               and '--dport 7070' in line and '-j ACCEPT' in line]
    if len(allowed) != 1 or '-s ' + previous + '/32 ' not in allowed[0]:
        raise ValueError('live_allowlist_differs_from_file')
    if command(['systemctl', 'is-active', 'powersystem-edge-firewall']).strip() != 'active':
        raise ValueError('firewall_not_active')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    backup = target.parent / ('ssh-admin-source-' + stamp)
    backup.mkdir(mode=0o700)
    shutil.copy2(target, backup / 'allowed-admin-ipv4.before')
    (backup / 'iptables-before.rules').write_text(before)
    os.chmod(backup / 'iptables-before.rules', 0o600)
    temporary = target.with_name(target.name + '.' + stamp + '.tmp')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        stream.write(source + '\n')
    os.replace(temporary, target)
    try:
        command(['systemctl', 'restart', 'powersystem-edge-firewall'])
        after = command(['iptables-save'])
        expected = before.replace(allowed[0], allowed[0].replace(previous + '/32', source + '/32'))
        if normalized(after) != normalized(expected):
            raise ValueError('non_admin_firewall_change')
        (backup / 'iptables-after.rules').write_text(after)
        os.chmod(backup / 'iptables-after.rules', 0o600)
        print(json.dumps({'changed': True, 'single_source_only': True, 'other_rules_unchanged': True,
                          'backup': str(backup)}))
    except BaseException:
        shutil.copy2(backup / 'allowed-admin-ipv4.before', target)
        command(['systemctl', 'restart', 'powersystem-edge-firewall'])
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Persist only this authenticated source for port 7070')
    main(parser.parse_args().apply)
