#!/usr/bin/env python3
"""Archive the active frontend image and verified application build sources.

Runs inside backup.sh's deployment lock. Only data is copied; no release,
container, policy, credential or host registration is activated.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path('/opt/powersystem')
BACKUPS = Path('/var/backups/powersystem')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def capture(output):
    output = Path(output).resolve()
    if output.parent != BACKUPS or not output.name.startswith('powersystem-') or not output.is_dir():
        raise ValueError('owned_backup_staging_required')
    state = ROOT / 'runtime/production-stack'
    pointer = json.loads((state / 'active.json').read_text())
    if (state / 'pending.json').exists():
        raise ValueError('unfinished_release_blocks_backup')
    release = state / 'releases' / pointer['release']
    manifest = json.loads((release / 'manifest.json').read_text())
    # Use the controller's canonical digest, also for historical manifest formats.
    spec = importlib.util.spec_from_file_location('backup_controller', ROOT / 'stages/04-frontend/deploy/production-stack.py')
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    controller.accepted(release, pointer)
    sources = ROOT / 'runtime/source-builds'
    spec = importlib.util.spec_from_file_location('backup_sources', ROOT / 'stages/04-frontend/deploy/source-build.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    target = output / 'release-assets'
    target.mkdir(mode=0o700)
    copied = set()
    receipts = []
    services = ('frontend', 'api', 'agent-rag', 'ops-agent', 'security-agent')
    for service in services:
        row = json.loads(subprocess.check_output(['docker', 'inspect', 'powersystem-stage4-' + service + '-1']))[0]
        if row['Image'] != manifest['images'][service]:
            raise ValueError('backup_running_image_mismatch:' + service)
        image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', row['Image']]))[0]
        receipt = helper.verify_receipt(row['Image'], service, sources, image)
        source = sources / 'contexts' / receipt['source_sha256']
        if receipt['source_sha256'] not in copied:
            shutil.copytree(source, target / 'source-builds/contexts' / source.name)
            helper.verify(target / 'source-builds/contexts' / source.name)
            copied.add(receipt['source_sha256'])
        source_receipt = sources / 'receipts' / (row['Image'][7:] + '.json')
        destination = target / 'source-builds/receipts' / source_receipt.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_receipt, destination)
        receipts.append(receipt)
    image_path = target / 'frontend-image.tar'
    subprocess.run(['docker', 'image', 'save', '--output', str(image_path), manifest['images']['frontend']],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pg = ['docker', 'exec', 'powersystem-stage4-postgres-1', 'psql', '-U', 'powersystem',
          '-d', 'powersystem_agent_rag', '-Atqc']
    tables = subprocess.check_output(pg + ["SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"]).decode().splitlines()
    if not all(re.fullmatch(r'[a-z_][a-z0-9_]*', name) for name in tables):
        raise ValueError('rag_backup_table_name_invalid')
    rag_counts = {name: int(subprocess.check_output(pg + ['SELECT count(*) FROM public.' + name])) for name in tables}
    metadata = {'format': 'release-backup-assets/v1', 'release': pointer['release'],
                'manifest_sha256': pointer['manifest_sha256'], 'images': manifest['images'],
                'frontend_image': {'image': manifest['images']['frontend'], 'file': image_path.name,
                                   'bytes': image_path.stat().st_size, 'sha256': digest(image_path)},
                'source_builds': receipts, 'rag_table_counts': rag_counts,
                'restore_activation_forbidden': True,
                'scope': 'active frontend image and five verified application source/receipt bindings'}
    (target / 'manifest.json').write_text(json.dumps(metadata, indent=2))
    for path in target.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return {'release_assets_saved': True, 'release': pointer['release'],
            'verified_source_snapshots': len(copied), 'frontend_image_bytes': image_path.stat().st_size}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    print(json.dumps(capture(parser.parse_args().output)))
