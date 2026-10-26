"""Cold-migration asset transform for the PowerSystem VM (pure functions only).

This module computes the *candidate* asset catalog for the new VM. It never
writes a registry file, never calls Docker/SSH, never imports into ``ma_ops``
and never activates anything. The caller (controller, under its own gate) is
responsible for importing ``import_request`` and installing
``collector_registry``.

Inputs that cannot be derived offline are explicit parameters; nothing is
guessed:

``catalog``
    The full *current* registry document, exactly ``{"assets": [...],
    "private_assets": {...}}`` as produced by ``setup-m2-collector.py``.
    ``assets`` uses the real 13-field schema from ``ops_contract.ASSET_FIELDS``.
``host_identity``
    ``{"hostname", "machine_id_sha256", "connection_ref"}`` for the VM.
    ``machine_id_sha256`` must be the value the collector itself compares
    against ``sha256(/etc/machine-id bytes .strip())``; it is not derivable
    from the host profile document and must not be guessed.
``containers``
    Exactly 20 ``docker inspect``-derived records with the minimal fields
    ``{"service", "container_id", "image_id", "project"}``. Container IDs are
    actual new IDs; source IDs are never reused.
``services``
    ``{"unit", "fragment_hash"}`` for each required VM unit. The fragment hash
    is the current file hash; it cannot be guessed.
``verified_at``
    Timezone-aware ISO-8601 timestamp for newly enabled assets. A clock read is
    an external fact, so it is supplied by the caller.
``host_mounts``
    Optional ``{"alias", "path"}`` list for ``linux.get_disk_status``. Defaults
    to the two project-standard mounts; extra VM filesystems must be supplied.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops_contract import ASSET_FIELDS, ASSET_PATTERN, TOOLS, digest, validate_catalog

ASSET_SCOPE = 'powersystem'
COMPOSE_PROJECT = 'powersystem-stage4'
SOURCE_CONNECTION_REF = 'r730xd-ssh'
TARGET_HOSTNAME = 'powersystem-prod'
TARGET_CONNECTION_REF = 'powersystem-prod-ssh'
TARGET_HOST_ID = TARGET_HOSTNAME
DATABASE_NAME = 'powersystem'
SERVICE_UNITS = ('powersystem-web-primary-frpc.service',
                 'powersystem-web-secondary-frpc.service',
                 'powersystem-backup.service')
DEFAULT_MOUNTS = ({'alias': 'root-filesystem', 'path': '/'},
                  {'alias': 'project-filesystem', 'path': '/opt/powersystem'})
CORE_SERVICES = {'frontend', 'api', 'gateway', 'emqx', 'tdengine', 'postgres', 'redis',
                 'ai-predict', 'feishu-adapter', 'agent', 'agent-rag', 'qdrant'}
HEX64 = re.compile(r'[a-f0-9]{64}')
CONTAINER_ID = re.compile(r'[a-f0-9]{64}')
IMAGE_ID = re.compile(r'sha256:[a-f0-9]{64}')
UNIT = re.compile(r'[a-zA-Z0-9_.@-]+\.service')


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _copy_asset(asset):
    return {field: (list(asset[field]) if isinstance(asset[field], list) else asset[field])
            for field in ASSET_FIELDS}


def _copy_private(entry):
    return json.loads(json.dumps(entry, ensure_ascii=False))


def _capabilities(kind):
    return [tool for tool, (asset_kind, _) in TOOLS.items() if asset_kind == kind]


def _asset(asset_id, kind, display_name, parent_id, criticality, verified_at,
           connection_ref, identity, version=1, enabled=True):
    return {'asset_id': asset_id, 'kind': kind, 'display_name': display_name,
            'project_scope': ASSET_SCOPE, 'parent_id': parent_id, 'enabled': enabled,
            'criticality': criticality, 'environment': 'production', 'version': version,
            'verified_at': verified_at, 'connection_ref': connection_ref,
            'identity_hash': digest(identity), 'capabilities': _capabilities(kind)}


def _source_host(assets):
    hosts = [a for a in assets if a['kind'] == 'host' and
             a['connection_ref'] == SOURCE_CONNECTION_REF]
    _require(len(hosts) == 1, 'source_host_not_unique')
    return hosts[0]


def _validate_host_identity(host_identity, source_identity):
    _require(isinstance(host_identity, dict) and
             set(host_identity) == {'hostname', 'machine_id_sha256', 'connection_ref'},
             'invalid_host_identity')
    _require(host_identity['hostname'] == TARGET_HOSTNAME, 'migration_target_hostname_mismatch')
    _require(host_identity['connection_ref'] == TARGET_CONNECTION_REF,
             'migration_target_connection_ref_mismatch')
    _require(isinstance(host_identity['machine_id_sha256'], str) and
             HEX64.fullmatch(host_identity['machine_id_sha256']) is not None,
             'invalid_machine_id_sha256')
    _require(source_identity['machine_hash'] != host_identity['machine_id_sha256'],
             'migration_machine_identity_reused')
    return {'machine_hash': host_identity['machine_id_sha256'], 'project': COMPOSE_PROJECT}


def _validate_containers(containers, expected_services):
    _require(isinstance(containers, list) and len(containers) == 20,
             'twenty_actual_containers_required')
    seen = set()
    for item in containers:
        _require(isinstance(item, dict) and
                 set(item) == {'service', 'container_id', 'image_id', 'project'},
                 'invalid_container_inspect_record')
        _require(isinstance(item['service'], str) and
                 re.fullmatch(ASSET_PATTERN, item['service']) is not None,
                 'invalid_container_service')
        _require(item['service'] not in seen, 'duplicate_container_service')
        _require(CONTAINER_ID.fullmatch(item['container_id']) is not None,
                 'invalid_container_id')
        _require(IMAGE_ID.fullmatch(item['image_id']) is not None, 'invalid_image_id')
        _require(item['project'] == COMPOSE_PROJECT, 'container_project_scope_mismatch')
        seen.add(item['service'])
    _require(seen == set(expected_services), 'container_service_set_changed')


def _validate_services(services):
    _require(isinstance(services, list), 'invalid_service_records')
    units = {}
    for item in services:
        _require(isinstance(item, dict) and set(item) == {'unit', 'fragment_hash'},
                 'invalid_service_record')
        _require(isinstance(item['unit'], str) and UNIT.fullmatch(item['unit']) is not None,
                 'invalid_service_unit')
        _require(HEX64.fullmatch(item['fragment_hash']) is not None, 'invalid_fragment_hash')
        _require(item['unit'] not in units, 'duplicate_service_unit')
        units[item['unit']] = item['fragment_hash']
    _require(set(units) == set(SERVICE_UNITS), 'required_service_units_missing')
    return units


def _validate_verified_at(verified_at):
    _require(isinstance(verified_at, str) and verified_at, 'verified_at_required')
    stamp = datetime.fromisoformat(verified_at.replace('Z', '+00:00'))
    _require(stamp.tzinfo is not None, 'verified_at_timezone_required')


def _validate_mounts(host_mounts):
    mounts = DEFAULT_MOUNTS if host_mounts is None else host_mounts
    _require(isinstance(mounts, (list, tuple)) and mounts, 'invalid_host_mounts')
    out = []
    for item in mounts:
        _require(isinstance(item, dict) and set(item) == {'alias', 'path'},
                 'invalid_host_mount')
        _require(re.fullmatch(ASSET_PATTERN, item['alias']) is not None and
                 isinstance(item['path'], str) and item['path'].startswith('/'),
                 'invalid_host_mount')
        out.append({'alias': item['alias'], 'path': item['path']})
    return out


def _database_template(source_children, private):
    databases = [a for a in source_children if a['kind'] == 'database']
    _require(len(databases) == 1, 'source_database_catalog_incomplete')
    entry = private[databases[0]['asset_id']]
    identity = entry.get('identity')
    _require(identity == {'database': DATABASE_NAME}, 'source_database_identity_changed')
    network = entry.get('network')
    _require(isinstance(network, str) and network.startswith(COMPOSE_PROJECT + '_'),
             'source_database_network_unmapped')
    password_file = entry.get('password_file')
    _require(isinstance(password_file, str) and
             password_file.startswith('/etc/powersystem-ops-read/'),
             'source_database_password_file_unmapped')
    postgres = entry.get('postgres_container_asset')
    _require(isinstance(postgres, str), 'source_database_container_unmapped')
    return {'network': network, 'password_file': password_file, 'postgres': postgres}


def migrate_assets(catalog, host_identity, containers, services, verified_at,
                   host_mounts=None):
    """Return the complete VM catalog, import request and collector registry.

    The result is inert data. Nothing is imported, activated or written.
    """
    _require(isinstance(catalog, dict) and
             set(catalog) == {'assets', 'private_assets'}, 'invalid_catalog_document')
    old_assets = catalog['assets']
    private = catalog['private_assets']
    _require(isinstance(private, dict), 'invalid_catalog_document')
    indexed = validate_catalog(old_assets)
    _require(set(private) == set(indexed), 'private_asset_set_mismatch')

    source = _source_host(old_assets)
    source_children = [a for a in old_assets if a['parent_id'] == source['asset_id']]
    _require(source_children, 'source_host_without_children')
    _require(TARGET_HOST_ID not in indexed, 'target_host_id_already_registered')

    for asset in old_assets:
        entry = private[asset['asset_id']]
        _require(isinstance(entry, dict) and isinstance(entry.get('identity'), dict),
                 'invalid_private_asset')
        _require(digest(entry['identity']) == asset['identity_hash'],
                 'private_asset_identity_mismatch')

    source_identity = private[source['asset_id']]['identity']
    _require(set(source_identity) == {'machine_hash', 'project'} and
             source_identity['project'] == COMPOSE_PROJECT, 'source_host_identity_changed')
    target_identity = _validate_host_identity(host_identity, source_identity)

    old_containers = [a for a in source_children if a['kind'] == 'container']
    _require(len(old_containers) == 20, 'source_container_catalog_incomplete')
    expected_services = {private[a['asset_id']]['identity'].get('service')
                         for a in old_containers}
    _require(all(isinstance(s, str) for s in expected_services) and len(expected_services) == 20,
             'source_container_identity_incomplete')
    _validate_containers(containers, expected_services)
    old_ids = {private[a['asset_id']]['identity']['container_id'] for a in old_containers}
    new_ids = {item['container_id'] for item in containers}
    _require(not (old_ids & new_ids), 'source_container_id_reused')
    _require(len(new_ids) == 20, 'duplicate_container_id')

    units = _validate_services(services)
    _validate_verified_at(verified_at)
    mounts = _validate_mounts(host_mounts)

    database = _database_template(source_children, private)
    _require('postgres' in expected_services, 'source_postgres_container_missing')
    postgres_asset = TARGET_HOST_ID + '-postgres'
    old_postgres = next(a for a in old_containers
                        if private[a['asset_id']]['identity'].get('service') == 'postgres')
    _require(database['postgres'] == old_postgres['asset_id'],
             'source_database_container_unmapped')

    disabled_ids = {source['asset_id']} | {a['asset_id'] for a in source_children}
    retained = [a for a in old_assets if a['asset_id'] not in disabled_ids]
    _require(len(retained) == len(old_assets) - len(disabled_ids),
             'source_asset_partition_invalid')

    assets = [_copy_asset(a) for a in retained]
    for asset in [_copy_asset(source)] + [_copy_asset(a) for a in source_children]:
        asset['enabled'] = False
        asset['version'] = indexed[asset['asset_id']]['version'] + 1
        assets.append(asset)

    added = [_asset(TARGET_HOST_ID, 'host', 'PowerSystem生产主机', None, 'core',
                    verified_at, TARGET_CONNECTION_REF, target_identity)]
    new_private = {}
    new_private[TARGET_HOST_ID] = {'identity': target_identity, 'mounts': mounts}
    new_container_ids = {}
    for item in containers:
        service = item['service']
        asset_id = TARGET_HOST_ID + '-' + service
        identity = {'container_id': item['container_id'], 'image_id': item['image_id'],
                    'project': item['project'], 'service': service}
        criticality = 'core' if service in CORE_SERVICES else 'support'
        added.append(_asset(asset_id, 'container', service + ' 项目容器', TARGET_HOST_ID,
                            criticality, verified_at, TARGET_CONNECTION_REF, identity))
        new_private[asset_id] = {'identity': identity}
        new_container_ids[asset_id] = item['container_id']
    for unit in SERVICE_UNITS:
        name = unit.removesuffix('.service')
        asset_id = TARGET_HOST_ID + '-' + name
        identity = {'unit': unit, 'fragment_hash': units[unit]}
        added.append(_asset(asset_id, 'service', name + ' 项目服务', TARGET_HOST_ID, 'core',
                            verified_at, TARGET_CONNECTION_REF, identity))
        new_private[asset_id] = {'identity': identity}
    database_identity = {'database': DATABASE_NAME}
    database_asset_id = TARGET_HOST_ID + '-business-db'
    added.append(_asset(database_asset_id, 'database', '平台PostgreSQL数据库', TARGET_HOST_ID,
                        'core', verified_at, TARGET_CONNECTION_REF, database_identity))
    new_private[database_asset_id] = {
        'identity': database_identity,
        'postgres_container_asset': postgres_asset,
        'network': database['network'], 'password_file': database['password_file']}
    assets.extend(added)

    result_private = {}
    for asset in assets:
        asset_id = asset['asset_id']
        result_private[asset_id] = (_copy_private(private[asset_id])
                                    if asset_id in private else _copy_private(new_private[asset_id]))

    validate_catalog(assets)
    ids = {a['asset_id'] for a in assets}
    _require(set(indexed) <= ids, 'complete_snapshot_requires_old_assets')
    for asset in assets:
        old = indexed.get(asset['asset_id'])
        if old is None:
            _require(asset['version'] == 1, 'new_asset_version_must_start_at_one')
        elif asset['asset_id'] in disabled_ids:
            _require(asset['version'] == old['version'] + 1,
                     'disabled_asset_version_must_advance_by_one')
        else:
            _require(asset == old, 'retained_asset_must_be_unchanged')
    for asset in assets:
        entry = result_private[asset['asset_id']]
        _require(isinstance(entry.get('identity'), dict) and
                 digest(entry['identity']) == asset['identity_hash'],
                 'private_asset_identity_mismatch')
    _require(result_private[TARGET_HOST_ID]['identity'] == target_identity,
             'target_identity_mismatch')
    postgres_asset = TARGET_HOST_ID + '-postgres'
    _require(result_private[database_asset_id]['postgres_container_asset'] == postgres_asset and
             new_container_ids[postgres_asset] ==
             next(c['container_id'] for c in containers if c['service'] == 'postgres'),
             'database_container_mapping_invalid')

    registry_hash = digest(assets)
    return {
        'format': 1,
        'project': COMPOSE_PROJECT,
        'import_request': {'assets': assets, 'registry_hash': registry_hash},
        'collector_registry': {'assets': assets, 'private_assets': result_private},
        'source_host_id': source['asset_id'],
        'target_host_id': TARGET_HOST_ID,
        'target_connection_ref': TARGET_CONNECTION_REF,
        'disabled_asset_ids': sorted(disabled_ids),
        'added_asset_ids': sorted(a['asset_id'] for a in added),
        'retained_asset_ids': sorted(a['asset_id'] for a in retained),
        'new_container_ids': new_container_ids,
        'registry_hash': registry_hash,
        'asset_count': len(assets),
        'manual_import_required': True,
        'auto_import': False,
        'auto_activate': False,
        'external_requirements': [
            'executor host mapping for ' + TARGET_CONNECTION_REF,
            'verified_at and fragment_hash supplied by the caller, never guessed',
            'import only after the controller cold-migration checkpoint',
        ],
    }
