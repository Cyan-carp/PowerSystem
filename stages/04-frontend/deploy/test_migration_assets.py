"""Offline tests for the cold-migration VM asset transform (no Docker/SSH/DB)."""
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    'migration_assets', Path(__file__).parents[2] / '09-ops-agent/deploy/migration-assets.py')
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)

PROJECT = 'powersystem-stage4'
SCOPE = 'powersystem'
STAMP = '2026-10-25T16:30:00+00:00'
SERVICES = ('api', 'frontend', 'gateway', 'emqx', 'tdengine', 'postgres', 'redis',
            'ai-predict', 'feishu-adapter', 'agent', 'agent-rag', 'qdrant', 'simulator',
            'ops-agent', 'security-agent', 'ops-executor', 'grafana', 'prometheus',
            'node-exporter', 'minio')
CORE = {'frontend', 'api', 'gateway', 'emqx', 'tdengine', 'postgres', 'redis',
        'ai-predict', 'feishu-adapter', 'agent', 'agent-rag', 'qdrant'}
TOOL_CAPS = {
    'host': ['linux.get_cpu_usage', 'linux.get_memory_usage', 'linux.get_disk_status',
             'linux.list_services', 'docker.list_containers'],
    'container': ['docker.inspect_container', 'docker.get_container_stats',
                  'docker.get_container_logs'],
    'service': ['linux.get_system_logs'],
    'database': ['postgresql.get_db_status', 'postgresql.get_connection_stats'],
}


def hexid(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def image(seed):
    return 'sha256:' + hashlib.sha256(('image-' + seed).encode()).hexdigest()


def asset(asset_id, kind, name, parent, identity, connection_ref='r730xd-ssh',
          version=1, enabled=True, criticality=None, verified_at=STAMP):
    return {'asset_id': asset_id, 'kind': kind, 'display_name': name,
            'project_scope': SCOPE, 'parent_id': parent, 'enabled': enabled,
            'criticality': criticality or ('support' if kind == 'container' else 'core'),
            'environment': 'production', 'version': version, 'verified_at': verified_at,
            'connection_ref': connection_ref, 'identity_hash': migration.digest(identity),
            'capabilities': list(TOOL_CAPS[kind])}


def old_catalog():
    assets, private = [], {}

    def add(item, entry):
        assets.append(item)
        private[item['asset_id']] = entry

    host_identity = {'machine_hash': 'a' * 64, 'project': PROJECT}
    add(asset('r730xd', 'host', 'R730XD项目运行主机', None, host_identity, version=4),
        {'identity': host_identity,
         'mounts': [{'alias': 'root-filesystem', 'path': '/'},
                    {'alias': 'project-filesystem', 'path': '/opt/powersystem'}]})
    for service in SERVICES:
        identity = {'container_id': hexid('old-' + service), 'image_id': image(service),
                    'project': PROJECT, 'service': service}
        add(asset('r730xd-' + service, 'container', service + ' 项目容器', 'r730xd', identity,
                  criticality='core' if service in CORE else 'support',
                  version=2 if service == 'api' else 1),
            {'identity': identity})
    for unit in ('powersystem-edge-frpc.service', 'powersystem-backup.service'):
        identity = {'unit': unit, 'fragment_hash': hexid('fragment-' + unit)}
        add(asset('r730xd-' + unit.removesuffix('.service'), 'service',
                  unit.removesuffix('.service') + ' 项目服务', 'r730xd', identity),
            {'identity': identity})
    database_identity = {'database': 'powersystem'}
    add(asset('r730xd-business-db', 'database', '平台PostgreSQL数据库', 'r730xd',
              database_identity),
        {'identity': database_identity, 'postgres_container_asset': 'r730xd-postgres',
         'network': PROJECT + '_default', 'password_file': '/etc/powersystem-ops-read/db-password'})

    standby_identity = {'machine_hash': 'b' * 64, 'project': PROJECT}
    add(asset('dl380-standby', 'host', 'DL380备用主机', None, standby_identity,
              connection_ref='dl380-ssh', version=2),
        {'identity': standby_identity,
         'mounts': [{'alias': 'root-filesystem', 'path': '/'}]})
    standby_container = {'container_id': 'd' * 64, 'image_id': image('standby'),
                         'project': PROJECT, 'service': 'api'}
    add(asset('dl380-standby-api', 'container', 'api 项目容器', 'dl380-standby',
              standby_container, connection_ref='dl380-ssh', criticality='core'),
        {'identity': standby_container})
    return {'assets': assets, 'private_assets': private}


def containers():
    return [{'service': service, 'container_id': hexid('new-' + service),
             'image_id': image('new-' + service), 'project': PROJECT}
            for service in SERVICES]


def services():
    return [{'unit': unit, 'fragment_hash': hexid('new-fragment-' + unit)}
            for unit in migration.SERVICE_UNITS]


def host_identity():
    return {'hostname': 'powersystem-prod', 'machine_id_sha256': 'e' * 64,
            'connection_ref': 'powersystem-prod-ssh'}


def run(catalog=None, host=None, container_list=None, service_list=None, **kwargs):
    return migration.migrate_assets(catalog or old_catalog(), host or host_identity(),
                                    container_list or containers(),
                                    service_list or services(), STAMP, **kwargs)


class MigrationAssetTests(unittest.TestCase):
    def test_new_host_and_containers_have_separate_identities(self):
        catalog = old_catalog()
        result = run(catalog)
        old_host = next(a for a in catalog['assets'] if a['asset_id'] == 'r730xd')
        new_host = next(a for a in result['import_request']['assets']
                        if a['asset_id'] == 'powersystem-prod')
        self.assertNotEqual(new_host['identity_hash'], old_host['identity_hash'])
        self.assertEqual(new_host['connection_ref'], 'powersystem-prod-ssh')
        self.assertEqual(new_host['parent_id'], None)
        old_ids = {e['identity']['container_id']
                   for e in catalog['private_assets'].values() if 'container_id' in e['identity']}
        new_ids = {e['identity']['container_id'] for aid, e in
                   result['collector_registry']['private_assets'].items()
                   if aid.startswith('powersystem-prod-') and 'container_id' in e['identity']}
        self.assertEqual(len(new_ids), 20)
        self.assertFalse(old_ids & new_ids)

    def test_old_source_assets_are_retained_but_disabled_and_bumped(self):
        catalog = old_catalog()
        result = run(catalog)
        by_id = {a['asset_id']: a for a in result['import_request']['assets']}
        old_by_id = {a['asset_id']: a for a in catalog['assets']}
        source_ids = {'r730xd'} | {a['asset_id'] for a in catalog['assets']
                                   if a['parent_id'] == 'r730xd'}
        for asset_id in source_ids:
            self.assertIn(asset_id, by_id)
            self.assertFalse(by_id[asset_id]['enabled'], asset_id)
            self.assertEqual(by_id[asset_id]['version'], old_by_id[asset_id]['version'] + 1,
                             asset_id)
        self.assertIn('r730xd', result['disabled_asset_ids'])
        self.assertEqual(by_id['r730xd']['verified_at'], old_by_id['r730xd']['verified_at'])

    def test_non_source_physical_host_assets_are_retained_unchanged(self):
        catalog = old_catalog()
        result = run(catalog)
        by_id = {a['asset_id']: a for a in result['import_request']['assets']}
        old_by_id = {a['asset_id']: a for a in catalog['assets']}
        for asset_id in ('dl380-standby', 'dl380-standby-api'):
            self.assertEqual(by_id[asset_id], old_by_id[asset_id])
            self.assertNotIn(asset_id, result['disabled_asset_ids'])
        self.assertEqual(result['collector_registry']['private_assets']['dl380-standby'],
                         catalog['private_assets']['dl380-standby'])

    def test_new_container_identity_matches_actual_inspect(self):
        result = run()
        assets = {a['asset_id']: a for a in result['import_request']['assets']}
        private = result['collector_registry']['private_assets']
        for item in containers():
            asset_id = 'powersystem-prod-' + item['service']
            self.assertEqual(private[asset_id]['identity'], item)
            self.assertEqual(assets[asset_id]['identity_hash'], migration.digest(item))
            self.assertEqual(result['new_container_ids'][asset_id], item['container_id'])
            self.assertEqual(assets[asset_id]['parent_id'], 'powersystem-prod')

    def test_database_asset_points_at_vm_postgres_container(self):
        result = run()
        private = result['collector_registry']['private_assets']
        database = private['powersystem-prod-business-db']
        self.assertEqual(database['postgres_container_asset'], 'powersystem-prod-postgres')
        self.assertEqual(database['identity'], {'database': 'powersystem'})
        self.assertEqual(database['network'], PROJECT + '_default')
        postgres = private['powersystem-prod-postgres']['identity']
        self.assertEqual(postgres['container_id'],
                         next(c['container_id'] for c in containers()
                              if c['service'] == 'postgres'))
        self.assertEqual(private['r730xd-business-db']['identity'], {'database': 'powersystem'})

    def test_service_assets_and_capability_whitelist(self):
        result = run()
        assets = {a['asset_id']: a for a in result['import_request']['assets']}
        for unit in migration.SERVICE_UNITS:
            asset_id = 'powersystem-prod-' + unit.removesuffix('.service')
            self.assertEqual(assets[asset_id]['kind'], 'service')
            self.assertEqual(assets[asset_id]['capabilities'], ['linux.get_system_logs'])
            self.assertEqual(assets[asset_id]['criticality'], 'core')
            self.assertNotIn('service.restart', assets[asset_id]['capabilities'])
        for item in assets.values():
            self.assertLessEqual(set(item['capabilities']), set(TOOL_CAPS[item['kind']]))

    def test_result_is_inert_and_import_contract_is_consistent(self):
        result = run()
        request = result['import_request']
        self.assertEqual(request['registry_hash'], migration.digest(request['assets']))
        self.assertEqual(result['registry_hash'], request['registry_hash'])
        self.assertEqual(result['collector_registry']['assets'], request['assets'])
        self.assertTrue(result['manual_import_required'])
        self.assertFalse(result['auto_import'])
        self.assertFalse(result['auto_activate'])
        self.assertNotIn('tasks', result)
        self.assertNotIn('approvals', result)
        migration.validate_catalog(request['assets'])

    def test_missing_or_guessed_inputs_are_rejected(self):
        catalog = old_catalog()
        broken = {'assets': catalog['assets']}
        with self.assertRaisesRegex(ValueError, 'invalid_catalog_document'):
            run(broken)
        with self.assertRaisesRegex(ValueError, 'migration_target_hostname_mismatch'):
            run(host={'hostname': 'r730xd', 'machine_id_sha256': 'e' * 64,
                      'connection_ref': 'powersystem-prod-ssh'})
        with self.assertRaisesRegex(ValueError, 'migration_machine_identity_reused'):
            run(host={'hostname': 'powersystem-prod', 'machine_id_sha256': 'a' * 64,
                      'connection_ref': 'powersystem-prod-ssh'})
        with self.assertRaisesRegex(ValueError, 'twenty_actual_containers_required'):
            run(container_list=containers()[:19])
        reused = containers()
        reused[0]['container_id'] = hexid('old-api')
        with self.assertRaisesRegex(ValueError, 'source_container_id_reused'):
            run(container_list=reused)
        with self.assertRaisesRegex(ValueError, 'required_service_units_missing'):
            run(service_list=services()[:2])
        with self.assertRaisesRegex(ValueError, 'verified_at_timezone_required'):
            migration.migrate_assets(catalog, host_identity(), containers(), services(),
                                     '2026-10-25T16:30:00')
        with self.assertRaisesRegex(ValueError, 'container_project_scope_mismatch'):
            wrong = containers()
            wrong[0]['project'] = 'other-project'
            run(container_list=wrong)


if __name__ == '__main__':
    unittest.main()
