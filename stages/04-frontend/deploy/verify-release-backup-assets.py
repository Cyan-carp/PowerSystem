#!/usr/bin/env python3
"""Verify restored release bytes offline; never start an app or activate keys."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import tarfile


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format') not in ('release-backup-assets/v1', 'release-backup-assets/v2') or manifest.get('restore_activation_forbidden') is not True:
        raise ValueError('release_assets_manifest_invalid')
    spec = importlib.util.spec_from_file_location('restored_sources', Path(__file__).with_name('source-build.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    seen = set()
    frontend = None
    for row in manifest['source_builds']:
        image = row['image']
        if not helper.IMAGE_ID.fullmatch(image) or manifest['images'].get(row['service']) != image:
            raise ValueError('restored_source_image_binding_invalid')
        path = root / 'source-builds/receipts' / (image[7:] + '.json')
        if digest(path) != row['receipt_sha256']:
            raise ValueError('restored_source_receipt_changed')
        receipt = json.loads(path.read_text())
        if any(receipt.get(k) != row[k] for k in ('image', 'service', 'source_sha256', 'revision')):
            raise ValueError('restored_source_receipt_binding_invalid')
        source_digest = row['source_sha256']
        if source_digest not in seen:
            info = helper.verify(root / 'source-builds/contexts' / source_digest)
            if info['sha256'] != source_digest or info['revision'] != row['revision']:
                raise ValueError('restored_source_manifest_invalid')
            seen.add(source_digest)
        if row['service'] == 'frontend':
            frontend = row
    expected_services = {'frontend', 'api', 'agent-rag', 'ops-agent', 'security-agent'}
    source_services = {r['service'] for r in manifest['source_builds']}
    manual = manifest.get('manual_images', []) if manifest['format'] == 'release-backup-assets/v2' else []
    manual_services = {r.get('service') for r in manual}
    if (source_services | manual_services != expected_services or
            source_services & manual_services or frontend is None or
            manual_services not in (set(), {'api'}) or len(manual_services) != len(manual)):
        raise ValueError('restored_release_sources_incomplete')
    for row in manual:
        if (row.get('service') != 'api' or row.get('image') != manifest['images'].get('api') or
                row.get('file') != 'api-image.tar' or row.get('source') != 'manual-api/build' or
                row.get('evidence_kind') != 'owner_accepted_manual_image' or
                not helper.IMAGE_ID.fullmatch(row['image'])):
            raise ValueError('restored_manual_image_binding_invalid')
        source = root / row['source']
        expected = row['source_files']
        if not {'server', 'Dockerfile', 'source-manifest.json', 'source/go.mod'} <= set(expected):
            raise ValueError('restored_manual_source_incomplete')
        for name in expected:
            relative = PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts or str(relative) != name:
                raise ValueError('restored_manual_source_path_invalid')
        actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
        if (source.is_symlink() or actual != set(expected) or
                any(p.is_symlink() for p in source.rglob('*'))):
            raise ValueError('restored_manual_source_set_changed')
        for name, record in expected.items():
            path = source / name
            if (not path.is_file() or path.stat().st_size != record['bytes'] or
                    digest(path) != record['sha256']):
                raise ValueError('restored_manual_source_changed')
        image_path = root / row['file']
        if image_path.stat().st_size != row['bytes'] or digest(image_path) != row['sha256']:
            raise ValueError('restored_manual_image_changed')
        with tarfile.open(image_path, 'r:') as archive:
            entries = json.load(archive.extractfile('manifest.json'))
            if len(entries) != 1:
                raise ValueError('restored_manual_image_ambiguous')
            config = archive.extractfile(entries[0]['Config']).read()
        if hashlib.sha256(config).hexdigest() != row['saved_config_sha256']:
            raise ValueError('restored_manual_image_config_changed')
    image = manifest['frontend_image']
    if image['file'] != 'frontend-image.tar' or image['image'] != frontend['image']:
        raise ValueError('restored_frontend_image_binding_invalid')
    path = root / image['file']
    if path.stat().st_size != image['bytes'] or digest(path) != image['sha256']:
        raise ValueError('restored_frontend_image_changed')
    with tarfile.open(path, 'r:') as archive:
        rows = json.load(archive.extractfile('manifest.json'))
        valid = False
        for row in rows:
            config = json.load(archive.extractfile(row['Config']))
            labels = config.get('config', {}).get('Labels') or {}
            if all(labels.get(k) == v for k, v in helper.labels_for(
                    {'sha256': frontend['source_sha256'], 'revision': frontend['revision']}, 'frontend').items()):
                valid = True
        if not valid:
            raise ValueError('restored_frontend_image_source_labels_invalid')
    return {'release_assets_verified': True, 'release': manifest['release'],
            'source_snapshots': len(seen), 'manual_images_verified': len(manual),
            'frontend_image_verified': True,
            'activation_forbidden': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    print(json.dumps(verify(parser.parse_args().source)))
