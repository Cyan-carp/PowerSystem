#!/usr/bin/env python3
"""Encrypted, dual-remote frontend release capsule for the controlled fast tier.

The capsule contains the pinned frontend image and frozen release files. Runtime
data is recovered from a verified full backup no older than 24 hours. It is not
a substitute for a database backup when an API, schema, or volume changes.
"""
import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('production_stack', HERE / 'production-stack.py')
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)


def checked(args, **kwargs):
    subprocess.run(args, check=True, timeout=600, **kwargs)


def target(path):
    value = Path(path).read_text(encoding='utf-8').strip()
    stack.require(bool(value) and '\n' not in value and '\r' not in value,
                  'fast_backup_target_missing')
    return value


def verify_stream(archive):
    """Read the whole decrypted gzip stream, including its trailing authentication."""
    decrypt = subprocess.Popen([
        'gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase-file',
        '/etc/powersystem/backup-passphrase', '--decrypt', str(archive)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    digests = {}
    metadata = None
    manifest = None
    try:
        with tarfile.open(fileobj=decrypt.stdout, mode='r|gz') as source:
            for item in source:
                if item.isfile():
                    stream = source.extractfile(item)
                    value = hashlib.sha256()
                    body = bytearray() if item.name in ('capsule.json', 'release/manifest.json') else None
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        value.update(block)
                        if body is not None:
                            body.extend(block)
                    digests[item.name] = value.hexdigest()
                    if item.name == 'capsule.json' and body is not None:
                        metadata = json.loads(body)
                    if item.name == 'release/manifest.json' and body is not None:
                        manifest = json.loads(body)
        while decrypt.stdout.read(1024 * 1024):
            pass
    finally:
        decrypt.stdout.close()
    stack.require(decrypt.wait(timeout=30) == 0 and metadata is not None and
                  manifest is not None and
                  {'capsule.json', 'frontend-image.tar', 'controller.py',
                   'release/manifest.json'} <=
                  set(digests),
                  'fast_capsule_unreadable')
    stack.require(stack.digest(manifest) == metadata['manifest_sha256'] and
                  digests['frontend-image.tar'] == metadata['image_tar_sha256'] and
                  digests['controller.py'] == metadata['controller_sha256'] and
                  all(digests.get('release/' + name) == expected
                      for name, expected in metadata['files'].items()),
                  'fast_capsule_payload_mismatch')
    return metadata


def make_capsule(name):
    stack.registered_business_host('backup')
    os.umask(0o077)
    stack.BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = stack.STATE_DIR / 'deployment.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stack.require(not (stack.STATE_DIR / 'pending.json').exists(),
                      'unfinished_release_requires_rollback')
        previous, manifest = stack.active()
        stack.current_matches(previous, manifest)
        candidate = stack.release(name)
        frozen = stack.immutable(candidate)
        stack.require(stack.sha(HERE / 'production-stack.py') == frozen['controller_sha256'],
                      'fast_capsule_controller_changed')
        stack.require(frozen.get('backup_tier') == stack.FAST_TIER and
                      frozen['parent'] == previous.name and
                      frozen['services'] == ['frontend'] and
                      stack.states(stack.inspect()) == frozen['before'],
                      'fast_capsule_candidate_not_current')
        baseline = stack.choose_fast_baseline(stack.read(stack.STATE_DIR / 'active.json'))
        primary = target('/etc/powersystem/backup-target')
        secondary = target('/etc/powersystem/backup-secondary-target')
        stack.require(primary != secondary, 'fast_backup_targets_must_differ')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        archive = stack.BACKUP_DIR / ('powersystem-frontend-' + stamp + '.tar.gz.gpg')
        stack.require(not archive.exists(), 'fast_capsule_name_collision')
        with tempfile.TemporaryDirectory(prefix='.frontend-capsule-',
                                         dir=stack.BACKUP_DIR) as temporary:
            temp = Path(temporary)
            image_tar = temp / 'frontend-image.tar'
            checked(['docker', 'save', '-o', str(image_tar), frozen['images']['frontend']])
            metadata = {'schema': 1, 'release': name, 'parent': previous.name,
                        'manifest_sha256': stack.digest(frozen),
                        'controller_sha256': frozen['controller_sha256'],
                        'frontend_image': frozen['images']['frontend'],
                        'baseline': baseline, 'image_tar_sha256': stack.sha(image_tar),
                        'files': frozen['files']}
            stack.write(temp / 'capsule.json', metadata)
            packed = temp / 'capsule.tar.gz'
            with tarfile.open(packed, 'w:gz') as output:
                output.add(temp / 'capsule.json', arcname='capsule.json')
                output.add(image_tar, arcname='frontend-image.tar')
                output.add(HERE / 'production-stack.py', arcname='controller.py')
                for relative in ['manifest.json', *frozen['files']]:
                    output.add(candidate / relative, arcname='release/' + relative)
            checked(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
                     '--passphrase-file', '/etc/powersystem/backup-passphrase',
                     '--symmetric', '--cipher-algo', 'AES256', '--output', str(archive),
                     str(packed)])
        verify_stream(archive)
        archive_sha = stack.sha(archive)
        checksum = Path(str(archive) + '.sha256')
        checksum.write_text(archive_sha + '  ' + archive.name + '\n', encoding='utf-8')
        checksum.chmod(0o600)
        receipt = {'schema': 1, 'status': 'ok', 'archive': archive.name,
                   'sha256': archive_sha, 'release': name, 'parent': previous.name,
                   'manifest_sha256': stack.digest(frozen),
                   'frontend_image': frozen['images']['frontend'],
                   'baseline': baseline, 'uploads': ['primary', 'secondary'],
                   'created_at': stack.now()}
        for destination in (primary, secondary):
            checked(['scp', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                     '-i', '/etc/powersystem/backup-ed25519',
                     str(archive), str(checksum), destination])
        receipt_path = Path(str(archive) + '.receipt.json')
        stack.write(receipt_path, receipt)
        print(json.dumps({'archive': str(archive), 'sha256': archive_sha,
                          'bytes': archive.stat().st_size, 'baseline': baseline['archive']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', required=True)
    args = parser.parse_args()
    make_capsule(args.id)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('fast backup failed: ' + type(error).__name__ + ': ' + str(error),
              file=sys.stderr)
        raise SystemExit(1)
