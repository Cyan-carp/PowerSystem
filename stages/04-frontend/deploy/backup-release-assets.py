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
import tarfile

ROOT = Path('/opt/powersystem')
BACKUPS = Path('/var/backups/powersystem')
MANUAL_API_IMAGE = 'sha256:19c12fd7e3ce9ff696d9b9a25e07bee76730db14909da0a780b28c5de92dfb9a'
MANUAL_API_BUILD = ROOT / 'runtime/quality-drift-blank-20261107-r1/build'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save_image(image, destination):
    inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
    if inspected['Id'] != image:
        raise ValueError('backup_image_identity_changed')
    subprocess.run(['docker', 'image', 'save', '--output', str(destination), image],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Docker's image ID is the SHA-256 of the saved config JSON.
    with tarfile.open(destination, 'r:') as archive:
        entries = json.load(archive.extractfile('manifest.json'))
        if len(entries) != 1:
            raise ValueError('backup_image_archive_ambiguous')
        config = archive.extractfile(entries[0]['Config']).read()
    saved = json.loads(config)
    if (saved.get('rootfs', {}).get('diff_ids') != inspected.get('RootFS', {}).get('Layers') or
            saved.get('config', {}).get('Labels') != inspected.get('Config', {}).get('Labels') or
            saved.get('config', {}).get('Entrypoint') != inspected.get('Config', {}).get('Entrypoint') or
            saved.get('config', {}).get('Cmd') != inspected.get('Config', {}).get('Cmd')):
        raise ValueError('backup_image_archive_config_mismatch')
    return {'image': image, 'file': destination.name,
            'bytes': destination.stat().st_size, 'sha256': digest(destination),
            'saved_config_sha256': hashlib.sha256(config).hexdigest()}


def capture_manual_api(target, image):
    if image != MANUAL_API_IMAGE or not MANUAL_API_BUILD.is_dir():
        raise ValueError('manual_api_provenance_unavailable')
    source = target / 'manual-api/build'
    shutil.copytree(MANUAL_API_BUILD, source, symlinks=False)
    files = {}
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('manual_api_source_link_forbidden')
        if path.is_file():
            name = path.relative_to(source).as_posix()
            files[name] = {'bytes': path.stat().st_size, 'sha256': digest(path)}
    if not {'server', 'Dockerfile', 'source-manifest.json', 'source/go.mod'} <= set(files):
        raise ValueError('manual_api_source_incomplete')
    saved = save_image(image, target / 'api-image.tar')
    return {'service': 'api', **saved, 'evidence_kind': 'owner_accepted_manual_image',
            'source': 'manual-api/build', 'source_files': files,
            'source_origin': str(MANUAL_API_BUILD)}


def capture(output):
    output = Path(output).resolve()
    if output.parent != BACKUPS or not output.name.startswith('powersystem-') or not output.is_dir():
        raise ValueError('owned_backup_staging_required')
    state = ROOT / 'runtime/production-stack'
    pointer = json.loads((state / 'active.json').read_text())
    release = state / 'releases' / pointer['release']
    manifest = json.loads((release / 'manifest.json').read_text())
    # Use the controller's canonical digest, also for historical manifest formats.
    spec = importlib.util.spec_from_file_location('backup_controller', ROOT / 'stages/04-frontend/deploy/production-stack.py')
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    controller.accepted(release, pointer)
    repair_evidence = None
    if (state/'pending.json').exists():
        proof_path, repair_evidence, _ = controller.telemetry_repair().backup_context(controller, v4_paused=True)
        release = controller.release(repair_evidence['failed_candidate'])
        manifest = controller.immutable(release)
    sources = ROOT / 'runtime/source-builds'
    spec = importlib.util.spec_from_file_location('backup_sources', ROOT / 'stages/04-frontend/deploy/source-build.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    target = output / 'release-assets'
    target.mkdir(mode=0o700)
    copied = set()
    receipts = []
    manual_images = []
    services = ('frontend', 'api', 'agent-rag', 'ops-agent', 'security-agent')
    for service in services:
        row = json.loads(subprocess.check_output(['docker', 'inspect', 'powersystem-stage4-' + service + '-1']))[0]
        if row['Image'] != manifest['images'][service]:
            raise ValueError('backup_running_image_mismatch:' + service)
        image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', row['Image']]))[0]
        try:
            receipt = helper.verify_receipt(row['Image'], service, sources, image)
        except helper.SourceError as error:
            if service != 'api' or str(error) != 'source_build_receipt_missing:api':
                raise
            manual_images.append(capture_manual_api(target, row['Image']))
            continue
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
    frontend_image = save_image(manifest['images']['frontend'], image_path)
    pg = ['docker', 'exec', 'powersystem-stage4-postgres-1', 'psql', '-U', 'powersystem',
          '-d', 'powersystem_agent_rag', '-Atqc']
    tables = subprocess.check_output(pg + ["SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"]).decode().splitlines()
    if not all(re.fullmatch(r'[a-z_][a-z0-9_]*', name) for name in tables):
        raise ValueError('rag_backup_table_name_invalid')
    rag_counts = {name: int(subprocess.check_output(pg + ['SELECT count(*) FROM public.' + name])) for name in tables}
    metadata = {'format': 'release-backup-assets/v2', 'release': release.name,
                'manifest_sha256': controller.digest(manifest), 'images': manifest['images'],
                'frontend_image': frontend_image,
                'source_builds': receipts, 'manual_images': manual_images,
                'rag_table_counts': rag_counts,
                'restore_activation_forbidden': True,
                'scope': 'active frontend image, four verified source builds, and exact manual API image with source evidence'}
    if repair_evidence:
        metadata.update(scope='frozen failed candidate actual images and sources; accepted pointer unchanged',
            basis='frozen_failed_candidate',active_release=pointer['release'],
            accepted_pointer=pointer, frozen_repair_sha256=controller.sha(proof_path),
            failed_candidate=repair_evidence['failed_candidate'])
    (target / 'manifest.json').write_text(json.dumps(metadata, indent=2))
    for path in target.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return {'release_assets_saved': True, 'release': pointer['release'],
            'verified_source_snapshots': len(copied), 'frontend_image_bytes': image_path.stat().st_size}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    print(json.dumps(capture(parser.parse_args().output)))
