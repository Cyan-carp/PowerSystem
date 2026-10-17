#!/usr/bin/env python3
"""Frozen production deployments. No build, pull, migrations or volume removal."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

ROOT = Path('/opt/powersystem')
STATE_DIR = ROOT / 'runtime/production-stack'
PROJECT = 'powersystem-stage4'
FILES = (
    'stages/04-frontend/deploy/compose.yaml',
    'stages/07-agent/deploy/agent.compose.yaml',
    'stages/07-agent/deploy/search.compose.yaml',
    'stages/08-agent-rag/deploy/production.compose.yaml',
    'stages/09-ops-agent/deploy/production.compose.yaml',
)
APPLICATIONS = {'api', 'frontend', 'agent', 'agent-rag', 'ops-agent',
                'security-agent', 'ops-executor', 'simulator', 'feishu-adapter',
                'gateway', 'ai-predict'}
ID = re.compile(r'sha256:[a-f0-9]{64}')
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')


class Blocked(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def command(args, log=None):
    result = subprocess.run(args, capture_output=True, timeout=600)
    if log:
        Path(log).write_bytes(result.stdout + result.stderr)
        Path(log).chmod(0o600)
    require(result.returncode == 0, 'command_failed:' + Path(args[0]).name)
    return result.stdout


def release(name):
    require(bool(NAME.fullmatch(name)), 'invalid_release_name')
    path = (STATE_DIR / 'releases' / name).resolve()
    require(path.parent == (STATE_DIR / 'releases').resolve(), 'invalid_release_path')
    return path


def owned(path):
    path = Path(path).resolve()
    require(path.is_relative_to(ROOT) and path.is_file(), 'owned_file_required')
    return path


def inspect():
    ids = command(['docker', 'ps', '-aq', '--filter',
                   'label=com.docker.compose.project=' + PROJECT]).decode().split()
    require(bool(ids), 'running_project_required')
    rows = json.loads(command(['docker', 'inspect', *ids]))
    result = {}
    for row in rows:
        labels = row['Config']['Labels']
        if labels.get('com.docker.compose.oneoff') == 'True':
            continue
        service = labels['com.docker.compose.service']
        require(service not in result, 'duplicate_service')
        result[service] = row
    return result


def states(rows):
    return {s: {'id': r['Id'], 'image': r['Image'],
                'started_at': r['State']['StartedAt'], 'restarts': r['RestartCount']}
            for s, r in rows.items()}


def healthy(rows):
    require(all(r['State']['Running'] and
                r['State'].get('Health', {}).get('Status', 'healthy') == 'healthy'
                for r in rows.values()), 'unhealthy_service')


def origin_args(path):
    args = read(owned(path))
    require(isinstance(args, list) and args[:2] == ['docker', 'compose'],
            'compose_arguments_required')
    files, env, index = [], None, 2
    while index < len(args):
        flag = args[index]
        require(index + 1 < len(args), 'invalid_compose_arguments')
        value = args[index + 1]
        if flag == '-f':
            files.append(str(owned(value)))
        elif flag == '--env-file':
            require(env is None, 'duplicate_environment_file')
            env = str(owned(value))
        else:
            require(flag == '--profile' and value == 'agent', 'unsupported_compose_argument')
        index += 2
    require(env and files[:5] == [str(ROOT / name) for name in FILES],
            'complete_ordered_stage4_7_8_9_stack_required')
    require(Path(env).is_relative_to(ROOT / 'runtime'), 'integrated_private_environment_required')
    return args, env


def validate(config):
    require(config.get('name') == PROJECT, 'wrong_project')
    services = config['services']
    require(APPLICATIONS.issubset(services), 'missing_production_service')
    env = services['api'].get('environment', {})
    for key, value in {
        'AGENT_ENABLED': 'true', 'AGENT_URL': 'http://agent:8092',
        'AGENT_CHAT_URL': 'http://agent-rag:8093', 'OPS_URL': 'http://ops-agent:8094',
        'AGENT_SERVICE_TOKEN_FILE': '/run/secrets/agent_service_token',
        'OPS_SERVICE_TOKEN_FILE': '/run/ma-api/ops-token',
        'MA_ACTOR_KEY_FILE': '/run/ma-api/actor-key',
    }.items():
        require(env.get(key) == value, 'missing_api_configuration:' + key)
    for key in ('AGENT_CHAT_TIMEOUT_SECONDS', 'AGENT_CHAT_INPUT_CHARS',
                'AGENT_CHAT_RATE', 'AGENT_CHAT_CONCURRENCY'):
        require(str(env.get(key, '')).isdigit() and int(env[key]) > 0,
                'missing_chat_limit:' + key)
    require(any(m.get('target') == '/run/ma-api' and m.get('read_only')
                for m in services['api'].get('volumes', [])), 'api_identity_mount_required')
    require(any(s['source'] == 'agent_service_token'
                for s in services['api'].get('secrets', [])), 'agent_token_mount_required')
    ops = services['ops-agent'].get('environment', {})
    require(ops.get('OPS_WRITE_ENABLED') == 'false' and
            ops.get('OPS_RUNTIME_PROFILE') == 'm4-server-readonly' and
            ops.get('OPS_SECURITY_ENABLED') == 'true', 'production_readonly_gates_required')
    for service in ('api', 'agent', 'agent-rag', 'ops-agent', 'security-agent', 'ops-executor'):
        require(not services[service].get('ports'), 'internal_service_port_forbidden')


def references(config, envfile):
    paths = {Path(envfile)}
    for item in config.get('secrets', {}).values():
        if 'file' in item:
            paths.add(Path(item['file']))
    for service in config['services'].values():
        for mount in service.get('volumes', []):
            if mount.get('type') == 'bind' and mount['target'].startswith('/run/'):
                source = Path(mount['source'])
                require(source.exists(), 'missing_private_mount')
                paths.update(source.rglob('*') if source.is_dir() else [source])
    # registry.json is a versioned asset ledger, refreshed after recreation.
    return {str(p): sha(p) for p in sorted(paths) if p.is_file() and p.name != 'registry.json'}


def escaped(value):
    if isinstance(value, str):
        return value.replace('$', '$$')
    if isinstance(value, list):
        return [escaped(v) for v in value]
    if isinstance(value, dict):
        # Compose already escapes command/entrypoint dollar signs on export.
        return {k: v if k in ('command', 'entrypoint') else escaped(v)
                for k, v in value.items()}
    return value


def preserve_storage_and_ports(config, rows, services):
    for service in services:
        value = config['services'][service]
        planned_ports = {}
        for port in value.get('ports', []):
            key = str(port['target']) + '/' + port.get('protocol', 'tcp')
            planned_ports.setdefault(key, []).append({
                'HostIp': port.get('host_ip', '0.0.0.0'), 'HostPort': str(port['published'])})
        require(planned_ports == (rows[service]['HostConfig'].get('PortBindings') or {}),
                'port_change_requires_separate_plan:' + service)
        planned_volumes = {
            mount['target']: config['volumes'][mount['source']]['name']
            for mount in value.get('volumes', []) if mount['type'] == 'volume'}
        actual_volumes = {mount['Destination']: mount['Name']
                          for mount in rows[service]['Mounts'] if mount['Type'] == 'volume'}
        require(planned_volumes == actual_volumes, 'persistent_volume_change_forbidden:' + service)


def compose(path):
    manifest = read(path / 'manifest.json')
    return ['docker', 'compose', '--project-name', PROJECT, '--env-file',
            manifest['origin_environment'], '-f', str(path / 'private/compose.json'),
            '--profile', 'agent']


def immutable(path):
    m = read(path / 'manifest.json')
    require(m['manifest_version'] == 1, 'unknown_manifest_version')
    for name, expected in m['files'].items():
        require(sha(path / name) == expected, 'frozen_artifact_changed')
    for name, expected in m['private_references'].items():
        require(sha(name) == expected, 'private_configuration_changed')
    for image in m['images'].values():
        require(ID.fullmatch(image) and
                json.loads(command(['docker', 'image', 'inspect', image]))[0]['Id'] == image,
                'fixed_local_image_required')
    return m


def accepted(path, pointer):
    m = immutable(path)
    require(digest(m) == pointer['manifest_sha256'], 'active_manifest_changed')
    gate = read(path / pointer['gate_file'])
    require(gate['passed'] and gate['manifest_sha256'] == digest(m), 'accepted_release_required')
    return path, m


def active():
    pointer = read(STATE_DIR / 'active.json')
    return accepted(release(pointer['release']), pointer)


def freeze(name, args_path, parent, services, overrides):
    args, env = origin_args(args_path)
    rows = inspect()
    healthy(rows)
    config = json.loads(command(args + ['config', '--format', 'json']))
    validate(config)
    require(set(rows) == set(config['services']) - {'backup-helper'}, 'project_service_set_changed')
    # Pin image-declared anonymous data volumes too; never silently replace them.
    for service, row in rows.items():
        mounts = config['services'][service].get('volumes', [])
        targets = {mount['target'] for mount in mounts}
        for mount in row['Mounts']:
            if mount['Type'] == 'volume' and mount['Destination'] not in targets:
                key = 'retained-' + service + '-' + digest(mount['Destination'])[:8]
                config.setdefault('volumes', {})[key] = {'name': mount['Name'], 'external': True}
                retained = {'type': 'volume', 'source': key,
                            'target': mount['Destination'], 'volume': {}}
                if not mount['RW']:
                    retained['read_only'] = True
                mounts.append(retained)
                config['services'][service]['volumes'] = mounts
    preserve_storage_and_ports(config, rows, set(rows))
    for service, row in rows.items():
        if service in services:
            continue
        actual_env = dict(item.split('=', 1) for item in row['Config']['Env'])
        require(all(actual_env.get(k) == str(v) for k, v in
                    config['services'][service].get('environment', {}).items()),
                'undeclared_environment_change:' + service)
    for service, value in config['services'].items():
        if service in rows:
            image = rows[service]['Image']
        else:
            image = json.loads(command(['docker', 'image', 'inspect', value['image']]))[0]['Id']
        if service in overrides:
            image = json.loads(command(['docker', 'image', 'inspect', overrides[service]]))[0]['Id']
        require(bool(ID.fullmatch(image)), 'image_id_required')
        value['image'] = image
        value.pop('build', None)
        value.pop('pull_policy', None)
    path = release(name)
    require(not path.exists(), 'release_already_exists')
    path.mkdir(parents=True, mode=0o700)
    write(path / 'private/compose.json', escaped(config))
    write(path / 'private/origin-args.json', args)
    for source, target in [
        (ROOT / '部署/独立云服务器部署/scripts/check-auth-wss.py', path / 'gate.py'),
        (ROOT / 'stages/09-ops-agent/deploy/ma-release.py', path / 'registry-helper.py'),
    ]:
        shutil.copyfile(source, target)
        target.chmod(0o600)
    files = ['private/compose.json', 'private/origin-args.json', 'gate.py', 'registry-helper.py']
    m = {'manifest_version': 1, 'release': name, 'created_at': now(), 'parent': parent,
         'controller_sha256': sha(__file__),
         'services': sorted(services), 'origin_environment': env,
         'images': {s: v['image'] for s, v in config['services'].items()},
         'files': {n: sha(path / n) for n in files},
         'private_references': references(config, env), 'before': states(rows)}
    write(path / 'manifest.json', m)
    # Docker parses the frozen JSON but no container or HTTP check is run here.
    expanded = json.loads(command(compose(path) + ['config', '--format', 'json']))
    require(expanded == config, 'frozen_compose_roundtrip_mismatch')
    return path, m


def set_active(path, gate_name):
    m = read(path / 'manifest.json')
    pointer = {'release': path.name, 'manifest_sha256': digest(m),
               'gate_file': gate_name, 'updated_at': now()}
    write(path / 'acceptance-pointer.json', pointer)
    write(STATE_DIR / 'active.json', pointer)


def current_matches(path, m):
    gate = read(path / read(STATE_DIR / 'active.json')['gate_file'])
    require(states(inspect()) == gate['states'], 'running_baseline_drift')


def bootstrap(a):
    require(not (STATE_DIR / 'active.json').exists(), 'baseline_already_registered')
    evidence, baseline = read(owned(a.evidence)), read(owned(a.baseline_state))
    require(evidence.get('passed') is True and
            evidence.get('production_unchanged_after_isolated_restore') is True,
            'completed_existing_acceptance_required')
    require(states(inspect()) == baseline['states'], 'accepted_baseline_drift')
    require(evidence['api_image'] == baseline['states']['api']['image'], 'acceptance_image_mismatch')
    path, m = freeze(a.id, a.from_args, None, [], {})
    require(m['before'] == baseline['states'], 'baseline_changed_during_registration')
    write(path / 'existing-acceptance.json', {
        'passed': True, 'manifest_sha256': digest(m), 'states': m['before'],
        'kind': 'adopted_existing_acceptance', 'recorded_at': now(),
        'evidence_sha256': sha(a.evidence), 'baseline_sha256': sha(a.baseline_state),
        'evidence_source': str(owned(a.evidence)), 'tests_rerun': False})
    set_active(path, 'existing-acceptance.json')


def prepare(a):
    previous, pm = active()
    current_matches(previous, pm)
    services = set(a.service or [])
    overrides = {}
    for item in a.image or []:
        require('=' in item, 'image_requires_service_equals_reference')
        service, image = item.split('=', 1)
        require(service not in overrides and bool(image), 'duplicate_or_empty_image')
        overrides[service] = image
        services.add(service)
    require(bool(services) and services.issubset(APPLICATIONS), 'explicit_application_services_required')
    args_path = a.from_args or previous / 'private/origin-args.json'
    freeze(a.id, args_path, previous.name, services, overrides)


def drain():
    count = command(['docker', 'exec', PROJECT + '-postgres-1', 'psql', '-U', 'powersystem',
                     '-d', 'ma_ops', '-Atqc', "SELECT count(*) FROM ma_tasks WHERE status IN "
                     "('queued','planning','executing','awaiting_approval')"]).decode().strip()
    require(count == '0', 'inflight_tasks_block_release')


def refresh_registry(path, envfile):
    before = states(inspect())['ops-executor']
    registry = Path('/etc/powersystem-ops-read/registry.json')
    ids_before = {v['asset_id'] for v in read(registry)['assets']}
    spec = importlib.util.spec_from_file_location('frozen_registry_helper', path / 'registry-helper.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.write
    def redirected(destination, value):
        if Path(destination).name == 'asset-refresh.json' or Path(destination).name.startswith('asset-import-'):
            destination = path / 'events' / (str(__import__('time').time_ns()) + '-' + Path(destination).name)
            Path(destination).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return original(destination, value)
    module.write = redirected
    module.refresh_assets(Path(envfile).parent.parent)
    after = states(inspect())['ops-executor']
    require(all(after[k] == before[k] for k in ('id', 'image', 'restarts')), 'executor_identity_changed')
    require({v['asset_id'] for v in read(registry)['assets']} == ids_before, 'new_asset_forbidden')


def deploy(path, services):
    m = immutable(path)
    command(compose(path) + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                            '--force-recreate', '--wait', '--wait-timeout', '120', *services],
            path / ('up-' + str(__import__('time').time_ns()) + '.log'))
    if {'api', 'frontend'} & set(services):
        command(['docker', 'exec', PROJECT + '-frontend-1', 'nginx', '-t'])
        command(['docker', 'exec', PROJECT + '-frontend-1', 'nginx', '-s', 'reload'])
    refresh_registry(path, m['origin_environment'])


def gate(path, services, before):
    m = immutable(path)
    rows = inspect()
    healthy(rows)
    require(set(rows) == set(before), 'service_set_changed')
    for service, row in rows.items():
        require(row['Image'] == m['images'][service], 'unexpected_image:' + service)
        if service not in services:
            expected = before[service]
            actual = states({service: row})[service]
            keys = ('id', 'image', 'restarts') if service == 'ops-executor' else expected.keys()
            require(all(actual[k] == expected[k] for k in keys), 'unrelated_service_changed:' + service)
    config = json.loads(command(compose(path) + ['config', '--format', 'json']))
    validate(config)
    for service in rows:
        actual = dict(v.split('=', 1) for v in rows[service]['Config']['Env'])
        require(all(actual.get(k) == str(v) for k, v in
                    config['services'][service].get('environment', {}).items()),
                'runtime_environment_mismatch:' + service)
    command([sys.executable, str(path / 'gate.py'), '/etc/powersystem/demo-password'],
            path / ('https-wss-' + str(__import__('time').time_ns()) + '.log'))
    final_rows = inspect()
    healthy(final_rows)
    require(states(final_rows) == states(rows), 'runtime_changed_during_acceptance')
    immutable(path)
    name = 'accepted-' + str(__import__('time').time_ns()) + '.json'
    write(path / name, {'passed': True, 'manifest_sha256': digest(m), 'states': states(rows),
                       'kind': 'automatic_https_wss_gate', 'accepted_at': now()})
    return name


def restore_transaction(transaction):
    previous = release(transaction['previous'])
    accepted(previous, read(previous / 'acceptance-pointer.json'))
    services = transaction['services']
    drain()
    deploy(previous, services)
    gate_name = gate(previous, services, transaction['before'])
    set_active(previous, gate_name)
    write(STATE_DIR / ('rollback-' + str(__import__('time').time_ns()) + '.json'),
          {'passed': True, 'transaction': transaction, 'restored_at': now()})
    (STATE_DIR / 'pending.json').unlink()


def publish(a):
    previous, pm = active()
    current_matches(previous, pm)
    healthy(inspect())
    candidate = release(a.id)
    m = immutable(candidate)
    require(m['parent'] == previous.name, 'stale_or_unrelated_candidate')
    require(states(inspect()) == m['before'], 'candidate_baseline_drift')
    services = m['services']
    require(bool(services) and set(services).issubset(APPLICATIONS), 'invalid_release_services')
    archive = Path(a.backup).resolve()
    require(archive.parent == Path('/var/backups/powersystem') and
            bool(re.fullmatch(r'powersystem-\d{8}T\d{6}Z\.tar\.gz\.gpg', archive.name)),
            'owned_encrypted_backup_required')
    require(sha(archive) == Path(str(archive) + '.sha256').read_text().split()[0],
            'backup_checksum_mismatch')
    age = datetime.datetime.now().timestamp() - archive.stat().st_mtime
    require(0 <= age <= 86400, 'recent_predeployment_backup_required')
    require(archive.stat().st_mtime >= datetime.datetime.fromisoformat(m['created_at']).timestamp(),
            'backup_must_follow_candidate_preparation')
    drain()
    transaction = {'previous': previous.name, 'candidate': candidate.name,
                   'services': services, 'before': states(inspect()), 'started_at': now(),
                   'predeployment_backup': archive.name, 'backup_sha256': sha(archive)}
    write(STATE_DIR / 'pending.json', transaction)
    try:
        deploy(candidate, services)
        accepted = gate(candidate, services, transaction['before'])
        set_active(candidate, accepted)
        (STATE_DIR / 'pending.json').unlink()
    except Exception as error:
        write(candidate / ('failed-' + str(__import__('time').time_ns()) + '.json'),
              {'passed': False, 'error_type': type(error).__name__, 'at': now()})
        # Keep pending.json if rollback itself fails; subsequent releases stay blocked.
        restore_transaction(transaction)
        raise Blocked('release_failed_and_previous_release_restored') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='phase', required=True)
    seed = sub.add_parser('bootstrap')
    for option in ('id', 'from-args', 'evidence', 'baseline-state'):
        seed.add_argument('--' + option, required=True)
    plan = sub.add_parser('prepare')
    plan.add_argument('--id', required=True)
    plan.add_argument('--from-args')
    plan.add_argument('--service', action='append')
    plan.add_argument('--image', action='append')
    activate = sub.add_parser('publish')
    activate.add_argument('--id', required=True)
    activate.add_argument('--backup', required=True)
    sub.add_parser('rollback')
    sub.add_parser('status')
    a = parser.parse_args()
    require(socket.gethostname() == 'R730XD' and os.geteuid() == 0, 'specified_root_business_host_required')
    os.umask(0o077)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl
    with (STATE_DIR / 'deployment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = STATE_DIR / 'pending.json'
        if a.phase == 'status':
            pointer = read(STATE_DIR / 'active.json') if (STATE_DIR / 'active.json').exists() else None
            print(json.dumps({'active': pointer, 'pending': pending.exists()}))
            return
        if a.phase == 'rollback':
            if not pending.exists():
                current, cm = active()
                current_matches(current, cm)
                require(bool(cm['parent']) and bool(cm['services']), 'no_previous_release')
                accepted(release(cm['parent']), read(release(cm['parent']) / 'acceptance-pointer.json'))
                write(pending, {'previous': cm['parent'], 'candidate': current.name,
                                'services': cm['services'], 'before': states(inspect()),
                                'started_at': now(), 'kind': 'explicit_parent_rollback'})
            restore_transaction(read(pending))
        else:
            require(not pending.exists(), 'unfinished_release_requires_rollback')
            globals()[a.phase](a)
    print(json.dumps({'phase': a.phase, 'completed': True,
                      'verification_run': a.phase in ('publish', 'rollback')}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Avoid leaking Compose output, credentials, JWTs or private configuration.
        reason = str(error) if isinstance(error, Blocked) else type(error).__name__
        print(json.dumps({'completed': False, 'blocked': reason}), file=sys.stderr)
        raise SystemExit(1)
