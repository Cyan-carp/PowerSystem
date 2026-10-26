#!/usr/bin/env python3
"""Versioned build inputs and image receipts. Never deploys or starts services."""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile

STAGES = ('02-backend', '04-frontend', '08-agent-rag', '09-ops-agent', '10-security-agent')
SERVICES = {
    'api': ('stages/02-backend', 'stages/02-backend/Dockerfile'),
    'gateway': ('stages/02-backend', 'stages/02-backend/Dockerfile'),
    'frontend': ('stages/04-frontend', 'stages/04-frontend/Dockerfile'),
    'agent-rag': ('stages/08-agent-rag', 'stages/08-agent-rag/Dockerfile'),
    # Ops imports contracts from stages 08 and 10: its context is the full snapshot.
    'ops-agent': ('', 'stages/09-ops-agent/Dockerfile'),
    'security-agent': ('stages/10-security-agent', 'stages/10-security-agent/Dockerfile'),
}
REQUIRED = (
    'stages/02-backend/internal/api/agent_runs.go',
    'stages/02-backend/internal/api/agent_artifacts.go',
    'stages/04-frontend/src/views/AgentSchedulesView.vue',
    'stages/08-agent-rag/rag_agent/run_api.py',
    'stages/08-agent-rag/rag_agent/artifacts.py',
    'stages/09-ops-agent/ops_agent/schedule_contract.py',
    'stages/09-ops-agent/ops_contract.py',
    'stages/09-ops-agent/v4_joint.py',
    'stages/10-security-agent/review_contract.py',
    'stages/10-security-agent/security_agent/action_review.py',
    'stages/10-security-agent/security_agent/ops_review.py',
    'stages/10-security-agent/security_agent/schedule_review.py',
)
MANIFEST = 'source-manifest.json'
IMAGE_ID = re.compile(r'sha256:[a-f0-9]{64}')
REVISION = re.compile(r'[a-f0-9]{40}')
LABEL_SOURCE = 'io.powersystem.source.sha256'
LABEL_SERVICE = 'io.powersystem.source.service'
LABEL_REVISION = 'org.opencontainers.image.revision'


class SourceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SourceError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    require(not path.is_absolute() and '\\' not in name and
            '..' not in path.parts and str(path) == name, 'source_path_invalid')
    require(len(path.parts) >= 3 and path.parts[0] == 'stages' and
            path.parts[1] in STAGES, 'source_path_outside_scope')
    require(not any(part in {'.git', 'private', 'node_modules', '__pycache__',
                             'artifacts', '.venv'} for part in path.parts),
            'source_private_or_generated_path')
    require(not path.name.startswith('.env') or path.name == '.env.example',
            'source_private_environment')
    require(path.suffix.lower() not in {'.key', '.pem', '.p12', '.pfx'},
            'source_private_key_path')
    return path


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    require(result.returncode == 0, 'source_git_failed')
    return result.stdout


def export(repo, revision, store, overlays=(), deletions=()):
    """HEAD is never implicit; unlisted workspace edits are an error."""
    repo = Path(repo).resolve()
    require(bool(REVISION.fullmatch(revision)), 'source_full_revision_required')
    require(git(repo, 'rev-parse', revision+'^{commit}').decode().strip() == revision,
            'source_revision_unresolved')
    roots = ['stages/'+stage for stage in STAGES]
    dirty = git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--', *roots)
    changed = set()
    for item in dirty.split(b'\0'):
        if not item:
            continue
        require(b'R' not in item[:2] and b'C' not in item[:2],
                'source_rename_requires_explicit_delete_and_add')
        changed.add(item[3:].decode('utf-8'))
    overlays, deletions = set(overlays), set(deletions)
    require(not overlays & deletions and changed == overlays | deletions,
            'source_unreviewed_workspace_changes')
    payload = {}
    with tarfile.open(fileobj=io.BytesIO(git(repo, 'archive', '--format=tar', revision, *roots))) as archive:
        for member in archive:
            if member.isdir():
                continue
            safe_name(member.name)
            require(member.isfile(), 'source_links_forbidden')
            payload[member.name] = (archive.extractfile(member).read(), member.mode & 0o777)
    for name in overlays:
        safe_name(name)
        path = repo / name
        require(path.is_file() and not path.is_symlink() and
                path.resolve().is_relative_to(repo), 'source_overlay_invalid')
        data = path.read_bytes()
        # Reviewed text overlays have the same LF convention as git archive.
        if b'\0' not in data:
            data = data.replace(b'\r\n', b'\n')
        payload[name] = (data, payload.get(name, (None, 0o644))[1])
    for name in deletions:
        safe_name(name)
        require(name in payload and not (repo/name).exists(), 'source_deletion_invalid')
        del payload[name]
    manifest = {'format': 1, 'revision': revision,
                'reviewed_overlays': sorted(overlays), 'reviewed_deletions': sorted(deletions),
                'files': {name: {'sha256': sha(data), 'mode': mode}
                          for name, (data, mode) in sorted(payload.items())}}
    require(all(name in manifest['files'] for name in REQUIRED), 'source_v4_files_missing')
    source_digest = digest(manifest)
    destination = Path(store).resolve() / 'contexts' / source_digest
    require(not destination.exists(), 'source_snapshot_already_exists')
    destination.mkdir(parents=True)
    for name, (data, mode) in payload.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    write_new(destination/MANIFEST, manifest)
    return verify(destination)


def verify(source):
    source = Path(source).resolve()
    manifest_path = source / MANIFEST
    require(manifest_path.is_file() and not manifest_path.is_symlink(), 'source_manifest_missing')
    manifest = load(manifest_path)
    require(manifest.get('format') == 1 and
            bool(REVISION.fullmatch(manifest.get('revision', ''))), 'source_manifest_invalid')
    files = manifest.get('files', {})
    require(all(name in files for name in REQUIRED) and
            all(recipe in files for _, recipe in SERVICES.values()), 'source_v4_files_missing')
    actual = set()
    for path in source.rglob('*'):
        require(not path.is_symlink(), 'source_links_forbidden')
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(source).as_posix())
    require(actual == set(files), 'source_file_set_changed')
    for name, expected in files.items():
        safe_name(name)
        require(expected.get('mode') in {0o644, 0o755, 0o664}, 'source_mode_invalid')
        require(sha((source/name).read_bytes()) == expected['sha256'],
                'source_file_changed:'+name)
    return {'source': str(source), 'sha256': digest(manifest),
            'revision': manifest['revision'], 'files': len(files)}


def build_tar(source, service):
    """Docker receives precisely the verified bytes, never a mutable live tree."""
    verify(source)
    source = Path(source)
    manifest = load(source/MANIFEST)
    context, recipe = SERVICES[service]
    prefix = context+'/' if context else ''
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, expected in sorted(manifest['files'].items()):
            if not name.startswith(prefix):
                continue
            data = (source/name).read_bytes()
            require(sha(data) == expected['sha256'], 'source_changed_during_pack')
            member = tarfile.TarInfo(name[len(prefix):])
            member.size, member.mode, member.mtime = len(data), expected['mode'], 0
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue(), recipe[len(prefix):]


def labels_for(info, service):
    return {LABEL_SOURCE: info['sha256'], LABEL_REVISION: info['revision'], LABEL_SERVICE: service}


def verify_receipt(image, service, store, image_metadata):
    require(service in SERVICES and bool(IMAGE_ID.fullmatch(image)), 'source_image_invalid')
    store = Path(store).resolve()
    receipt_path = store / 'receipts' / (image[7:]+'.json')
    require(receipt_path.is_file() and not receipt_path.is_symlink(), 'source_build_receipt_missing:'+service)
    receipt = load(receipt_path)
    source_digest = receipt.get('source_sha256', '')
    require(bool(re.fullmatch(r'[a-f0-9]{64}', source_digest)), 'source_receipt_invalid')
    source = store / 'contexts' / source_digest
    require(not source.is_symlink(), 'source_links_forbidden')
    info = verify(source)
    require(info['sha256'] == source_digest and receipt.get('format') == 1 and
            receipt.get('image') == image and receipt.get('service') == service and
            receipt.get('revision') == info['revision'], 'source_receipt_binding_mismatch')
    labels = image_metadata.get('Config', {}).get('Labels') or {}
    require(image_metadata.get('Id') == image and
            all(labels.get(k) == v for k, v in labels_for(info, service).items()),
            'source_image_labels_mismatch')
    return {'service': service, 'image': image, 'source_sha256': source_digest,
            'revision': info['revision'], 'receipt_sha256': sha(receipt_path.read_bytes())}


def build(source, service, tag, store):
    info = verify(source)
    require(service in SERVICES, 'source_service_unknown')
    require(Path(source).resolve() == Path(store).resolve()/'contexts'/info['sha256'],
            'source_snapshot_outside_store')
    require(bool(re.fullmatch(r'[a-z0-9][a-z0-9._/-]*:[a-zA-Z0-9_.-]+', tag)) and
            tag.rsplit(':', 1)[1].lower() != 'latest', 'source_versioned_tag_required')
    # Never replace an existing tag or rely on one for receipt identity.
    previous = subprocess.run(['docker', 'image', 'inspect', tag], capture_output=True)
    require(previous.returncode != 0, 'source_tag_already_exists')
    data, recipe = build_tar(Path(source), service)
    args = ['docker', 'build', '--tag', tag, '--file', recipe]
    if service == 'gateway':
        args += ['--build-arg', 'APP=gateway']
    for key, value in labels_for(info, service).items():
        args += ['--label', key+'='+value]
    result = subprocess.run(args+['-'], input=data, stdout=sys.stderr, stderr=sys.stderr)
    require(result.returncode == 0, 'source_build_failed')
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag]))[0]
    image_id = image['Id']
    require(bool(IMAGE_ID.fullmatch(image_id)) and
            all((image['Config'].get('Labels') or {}).get(k) == v
                for k, v in labels_for(info, service).items()), 'source_built_labels_mismatch')
    receipt = {'format': 1, 'image': image_id, 'service': service,
               'source_sha256': info['sha256'], 'revision': info['revision'],
               'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    write_new(Path(store)/'receipts'/(image_id[7:]+'.json'), receipt)
    return verify_receipt(image_id, service, store, image)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export_parser = sub.add_parser('export')
    export_parser.add_argument('--repo', required=True)
    export_parser.add_argument('--revision', required=True)
    export_parser.add_argument('--store', required=True)
    export_parser.add_argument('--overlay', action='append', default=[])
    export_parser.add_argument('--delete', action='append', default=[])
    verify_parser = sub.add_parser('verify')
    verify_parser.add_argument('--source', required=True)
    build_parser = sub.add_parser('build')
    build_parser.add_argument('--source', required=True)
    build_parser.add_argument('--service', choices=sorted(SERVICES), required=True)
    build_parser.add_argument('--tag', required=True)
    build_parser.add_argument('--store', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'export':
            result = export(args.repo, args.revision, args.store, args.overlay, args.delete)
        elif args.command == 'verify':
            result = verify(args.source)
        else:
            result = build(args.source, args.service, args.tag, args.store)
        print(json.dumps(result, ensure_ascii=False))
    except (SourceError, OSError, ValueError, KeyError) as error:
        # Errors contain paths/reasons, never file contents or Docker config/env.
        print(json.dumps({'blocked': str(error)}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
