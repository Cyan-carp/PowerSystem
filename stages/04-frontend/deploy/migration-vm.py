"""Machine-bound cold migration requalification, invoked by production-stack only.

Source acceptance is retained as history. It never accepts new container IDs.
Cold-copy receipts travel only through the approved root management channel.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import time

DATA = {'postgres', 'redis', 'tdengine', 'emqx', 'qdrant'}
BACKGROUND = {'simulator', 'gateway', 'ai-predict', 'agent', 'feishu-adapter'}
TARGET_HOST = 'powersystem-prod-vm117'


def require_receipt_scope(ps, receipt, host):
    ps.require(receipt.get('format') == 1 and receipt.get('project') == ps.PROJECT,
               'migration_receipt_scope_invalid')
    ps.require(host['host_id'] == receipt.get('target_host_id') == TARGET_HOST,
               'migration_target_identity_mismatch')
    ps.require(receipt.get('source_host_id') == 'r730xd-source',
               'migration_source_identity_mismatch')
    ps.require(receipt.get('source_fenced') is True and
               receipt.get('source_services_stopped') is True,
               'migration_source_not_fenced')
    ps.require(len(receipt.get('volumes', {})) == 13 and
               all(v.get('verified') is True and v.get('source_sha256') ==
                   v.get('target_sha256') and v.get('source_sha256')
                   for v in receipt['volumes'].values()), 'migration_cold_volumes_unverified')
    ps.require(receipt.get('mutable_bindings_verified') is True and
               receipt.get('role_files_verified') is True and
               receipt.get('source_backup', {}).get('dual_copies_verified') is True,
               'migration_files_or_backup_unverified')
    ps.require(len(receipt.get('images', {})) == 20 and
               all(ps.ID.fullmatch(v) for v in receipt['images'].values()),
               'migration_fixed_images_required')
    ps.require(receipt.get('no_replay_pending') is True, 'migration_pending_work_requires_review')


def receipt_file(ps, filename):
    path = ps.owned(filename)
    ps.require(path.is_relative_to(ps.ROOT / 'runtime/pve-migration-20261025-r1'),
               'migration_receipt_directory_invalid')
    ps.require(not path.is_symlink() and path.stat().st_uid == 0 and
               not stat.S_IMODE(path.stat().st_mode) & 0o077,
               'migration_receipt_permissions_invalid')
    receipt = ps.read(path)
    require_receipt_scope(ps, receipt, ps.registered_business_host('migration'))
    return path, receipt


def remap_private(value, old, new):
    if isinstance(value, str):
        return value.replace(old + '/', new + '/') if value.startswith(old + '/') else value
    if isinstance(value, list):
        return [remap_private(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: remap_private(v, old, new) for k, v in value.items()}
    return value


def stage(ps, args):
    proof_path, proof = receipt_file(ps, args.receipt)
    ps.require(not ps.inspect_empty_allowed(), 'migration_target_project_already_exists')
    source_path = ps.release(proof['source_release'])
    original = ps.immutable(source_path)
    ps.require(ps.digest(original) == proof['source_manifest_sha256'],
               'migration_source_manifest_changed')
    ps.require(original['images'] == proof['images'], 'migration_image_set_changed')
    source_config = json.loads(ps.command(ps.compose(source_path) + ['config', '--format', 'json']))
    old = Path(original['origin_environment']).parent
    new = ps.ROOT / 'runtime/ma-m4-vm117-20261025-r1/private'
    ps.require(old.name == 'private' and new.is_dir() and old != new,
               'migration_new_private_directory_required')
    config = remap_private(source_config, str(old), str(new))
    database_hostname = proof['service_hostnames']['tdengine']
    ps.require(bool(__import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', database_hostname)),
               'migration_database_hostname_invalid')
    # TDengine persists this FQDN in its cold data; a new Docker ID must not
    # change that database identity. Node metrics identify the VM explicitly.
    host_overlay = {'services': {'tdengine': {'hostname': database_hostname},
                                 'node-exporter': {'hostname': 'powersystem-prod'}}}
    for service, overlay in host_overlay['services'].items():
        config['services'][service].update(overlay)
    # Images may declare anonymous state (Alertmanager does). Give every
    # inspected source volume an explicit external identity in the VM.
    host_overlay['volumes'] = {}
    for index, item in enumerate(proof['volume_mounts']):
        service, destination, name = item['service'], item['destination'], item['name']
        ps.require(name in proof['volumes'] and service in config['services'],
                   'migration_volume_mount_unverified')
        alias = 'migration-volume-' + str(index)
        mount = {'type': 'volume', 'source': alias, 'target': destination}
        mounts = config['services'][service].setdefault('volumes', [])
        existing = next((v for v in mounts if v['target'] == destination), None)
        if existing:
            ps.require(existing['type'] == 'volume', 'migration_volume_type_changed')
            mount.update({k: v for k, v in existing.items() if k not in ('source',)})
            mounts.remove(existing)
        mounts.append(mount)
        mounts.sort(key=lambda v: v['target'])
        config.setdefault('volumes', {})[alias] = {'name': name, 'external': True}
        host_overlay['volumes'][alias] = {'name': name, 'external': True}
        overlay = host_overlay['services'].setdefault(service, {})
        overlay.setdefault('volumes', []).append(mount)
    # Compose drops unused declarations when it normalizes a frozen document.
    used_volumes = {v['source'] for s in config['services'].values()
                    for v in s.get('volumes', []) if v['type'] == 'volume'}
    config['volumes'] = {k: v for k, v in config['volumes'].items() if k in used_volumes}
    origin = str(new / Path(original['origin_environment']).name)
    ps.validate(config)
    ps.require({s: v['image'] for s, v in config['services'].items()} == proof['images'],
               'migration_unexpected_image')
    ps.check_private_layout(config, origin)
    path = ps.release(args.id)
    ps.require(not path.exists(), 'release_already_exists')
    path.mkdir(parents=True, mode=0o700)
    ps.write(path / 'private/compose.json', ps.escaped(config))
    ps.write(path / 'host-identity.compose.json', host_overlay)
    from_args = remap_private(ps.read(source_path / 'private/origin-args.json'), str(old), str(new))
    from_args += ['-f', str(path / 'host-identity.compose.json')]
    ps.write(path / 'private/origin-args.json', from_args)
    for source, name in [(ps.ROOT / 'deploy/edge-server/独立云服务器部署/scripts/check-auth-wss.py', 'gate.py'),
                         (ps.ROOT / 'stages/09-ops-agent/deploy/ma-release.py', 'registry-helper.py'),
                         (proof_path, 'migration-receipt.json')]:
        shutil.copyfile(source, path / name)
        (path / name).chmod(0o600)
    names = ['private/compose.json', 'private/origin-args.json', 'host-identity.compose.json', 'gate.py',
             'registry-helper.py', 'migration-receipt.json']
    manifest = {'manifest_version': 1, 'release': path.name, 'parent': None,
                'created_at': ps.now(), 'backup_tier': ps.FULL_TIER, 'services': [],
                'origin_environment': origin, 'controller_sha256': ps.sha(ps.__file__),
                'images': proof['images'], 'source_builds': [], 'before': {},
                'private_references': ps.references(config, origin),
                'files': {name: ps.sha(path / name) for name in names},
                'migration': {'format': 1, 'source_release': source_path.name,
                              'receipt_sha256': ps.sha(proof_path),
                              'host_id': TARGET_HOST}}
    ps.write(path / 'manifest.json', manifest)
    roundtrip = json.loads(ps.command(ps.compose(path) + ['config', '--format', 'json']))
    ps.require(roundtrip == config, 'frozen_compose_roundtrip_mismatch')
    ps.immutable(path)
    # Old active is archived by the controller and cannot masquerade as VM acceptance.
    previous = ps.STATE_DIR / 'active.json'
    if previous.exists():
        pointer = ps.read(previous)
        ps.require(pointer['release'] == source_path.name and
                   pointer['manifest_sha256'] == proof['source_manifest_sha256'],
                   'migration_copied_pointer_mismatch')
        archive = ps.STATE_DIR / ('source-pointer-' + str(time.time_ns()) + '.json')
        os.replace(previous, archive)
    ps.command(ps.compose(path) + ['create', '--no-build', '--pull', 'never'])
    rows = ps.inspect()
    ps.require(set(rows) == set(proof['images']) and
               all(not r['State']['Running'] for r in rows.values()),
               'migration_create_must_not_start_services')
    ps.preserve_storage_and_ports(config, rows, set(rows))
    ps.write(path / 'migration-progress.json', {'stage': 'created', 'at': ps.now(),
                                               'source_fenced': True})


def validate_staged(ps, args):
    path = ps.release(args.id)
    manifest = ps.immutable(path)
    ps.require(manifest.get('migration', {}).get('host_id') ==
               ps.registered_business_host('migration')['host_id'] == TARGET_HOST,
               'migration_target_identity_mismatch')
    proof = ps.read(path / 'migration-receipt.json')
    require_receipt_scope(ps, proof, ps.registered_business_host('migration'))
    return path, manifest, proof


def start(ps, args):
    path, manifest, proof = validate_staged(ps, args)
    progress = ps.read(path / 'migration-progress.json')
    expected = {'data': 'created', 'readonly': 'data_verified', 'background': 'readonly_verified'}
    ps.require(progress['stage'] == expected[args.group], 'migration_phase_out_of_order')
    rows = ps.inspect()
    groups = {'data': DATA, 'readonly': set(rows) - DATA - BACKGROUND,
              'background': BACKGROUND}
    services = sorted(groups[args.group])
    log = path / ('migration-start-' + args.group + '.log')
    try:
        ps.command(ps.compose(path) + ['start', '--wait', '--wait-timeout', '120', *services], log)
    except ps.Blocked:
        diagnostic = log.read_text(errors='replace') if log.exists() else ''
        if 'api' not in services or not ps.api_startup_wait_diagnostic(diagnostic):
            raise
        ps.wait_initial_api(manifest, services)
    ps.write(path / 'migration-progress.json', {'stage': args.group + '_started',
                                               'at': ps.now(), 'source_fenced': True})


def checkpoint(ps, args):
    path, manifest, proof = validate_staged(ps, args)
    progress = ps.read(path / 'migration-progress.json')
    report = ps.read(ps.owned(args.evidence))
    expected = {'data': 'data_started', 'readonly': 'readonly_started'}
    ps.require(progress['stage'] == expected[args.group] and
               report.get('passed') is True and report.get('kind') == args.group and
               report.get('migration_receipt_sha256') == manifest['migration']['receipt_sha256'],
               'migration_checkpoint_unverified')
    if args.group == 'data':
        ps.require(report.get('databases_match') is True and report.get('queues_match') is True and
                   report.get('no_work_replayed') is True, 'migration_data_checkpoint_incomplete')
    else:
        ps.require(report.get('vm_assets_registered') is True and
                   report.get('old_host_disabled') is True and report.get('collector_verified') is True,
                   'migration_readonly_checkpoint_incomplete')
    ps.write(path / ('migration-checkpoint-' + args.group + '.json'),
             {'source': str(ps.owned(args.evidence)), 'sha256': ps.sha(args.evidence), 'report': report})
    ps.write(path / 'migration-progress.json', {'stage': args.group + '_verified', 'at': ps.now()})


def accept(ps, args):
    path, manifest, proof = validate_staged(ps, args)
    ps.require(ps.read(path / 'migration-progress.json')['stage'] == 'background_started',
               'migration_background_not_started')
    rows = ps.inspect()
    ps.healthy(rows)
    ps.require(all(r['Id'] != proof['source_states'][s]['id'] for s, r in rows.items()),
               'migration_cannot_reuse_source_container_identity')
    ps.preflight(ps.read(path / 'private/compose.json'), manifest['origin_environment'])
    ps.drain()
    ps.refresh_registry(path, manifest['origin_environment'])
    before = ps.states(ps.inspect())
    name = ps.gate(path, [], before)
    ps.write(path / 'migration-baseline.json', {'passed': True, 'new_host': TARGET_HOST,
              'source_release': proof['source_release'], 'states': before,
              'gate_file': name, 'accepted_at': ps.now(), 'kind': 'cold_migration_requalification'})
    ps.set_active(path, name)
    ps.write(path / 'migration-progress.json', {'stage': 'accepted', 'at': ps.now()})


def dispatch(ps, args):
    ps.require(ps.registered_business_host('migration')['host_id'] == TARGET_HOST,
               'migration_registered_target_required')
    {'migration-stage': stage, 'migration-start': start,
     'migration-checkpoint': checkpoint, 'migration-accept': accept}[args.phase](ps, args)
