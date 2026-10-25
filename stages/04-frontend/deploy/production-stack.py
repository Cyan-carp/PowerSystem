#!/usr/bin/env python3
"""Frozen production deployments. No build, pull, migrations or volume removal."""
import argparse
import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time

ROOT = Path('/opt/powersystem')
STATE_DIR = ROOT / 'runtime/production-stack'
TIMING_DIR = STATE_DIR / 'timing'
TIMING_KEEP = 200
LEDGER = Path('/etc/powersystem-ops-read/registry.json')
EXECUTOR_TARGET = '/run/ma-executor'
MIGRATION_DIR = '/app/deploy/postgres'
# Statements store.Migrate can execute after splitting on ';'. Anything else in a
# migration fragment is a startup failure that no rollback can repair cheaply.
MIGRATION_KEYWORDS = {
    'ALTER', 'ANALYZE', 'BEGIN', 'COMMENT', 'COMMIT', 'CREATE', 'DELETE', 'DO', 'DROP',
    'GRANT', 'INSERT', 'REVOKE', 'SELECT', 'SET', 'TRUNCATE', 'UPDATE', 'VACUUM', 'WITH',
}
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
CHUNK = re.compile(r'-[A-Za-z0-9_-]{6,12}\.(?:js|css)$')
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
BACKUP_DIR = Path('/var/backups/powersystem')
FAST_TIER = 'frontend-fast'
FULL_TIER = 'full'
MAX_FAST_BASELINE_AGE = 86400


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


def command(args, log=None, env=None):
    result = subprocess.run(args, capture_output=True, timeout=600, env=env)
    if log:
        Path(log).write_bytes(result.stdout + result.stderr)
        Path(log).chmod(0o600)
    require(result.returncode == 0, 'command_failed:' + Path(args[0]).name)
    return result.stdout


TIMELINE = None


class Span(dict):
    """One measured step; callers add bytes or counts inside the block."""

    def __init__(self, timeline, name):
        super().__init__()
        self.timeline = timeline
        self.name = name

    def __enter__(self):
        self.started = time.monotonic()
        self.offset = round(self.started - (self.timeline.t0 if self.timeline else self.started), 3)
        return self

    def __exit__(self, kind, error, trace):
        self['name'] = self.name
        self['offset'] = self.offset
        self['seconds'] = round(time.monotonic() - self.started, 3)
        self['ok'] = error is None
        if error is not None:
            self['error'] = str(error) if isinstance(error, Blocked) else type(error).__name__
        value = {key: item for key, item in self.items() if item is not None}
        if self.timeline is not None:
            self.timeline.spans.append(value)
        return False


def track(name):
    return Span(TIMELINE, name)


class Timeline:
    """Per-phase record of durations and bytes. Never stores command output."""

    def __init__(self, phase, release_name):
        self.phase = phase
        self.release = release_name
        self.t0 = time.monotonic()
        self.started_at = now()
        self.spans = []
        self.notes = {}

    def describe(self):
        slowest = sorted(({'name': span['name'], 'seconds': span['seconds']}
                          for span in self.spans), key=lambda item: item['seconds'],
                         reverse=True)[:5]
        value = {'schema': 1, 'phase': self.phase, 'release': self.release,
                 'started_at': self.started_at, 'finished_at': now(),
                 'total_seconds': round(time.monotonic() - self.t0, 3),
                 'host': socket.gethostname(), 'slowest': slowest,
                 'spans': self.spans, 'notes': self.notes}
        try:
            value['load1'] = float(Path('/proc/loadavg').read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pass
        return value


def persist_timing(status, error=None):
    if TIMELINE is None:
        return None, None
    value = TIMELINE.describe()
    value['status'] = status
    if error is not None:
        value['error'] = str(error) if isinstance(error, Blocked) else type(error).__name__
    try:
        path = TIMING_DIR / (str(time.time_ns()) + '-' + value['phase'] + '.json')
        write(path, value)
    except OSError:
        # Measurement must never decide the release outcome.
        return None, value
    for stale in sorted(TIMING_DIR.glob('*.json'))[:-TIMING_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path, value


def recent_timings(limit):
    rows = []
    for path in sorted(TIMING_DIR.glob('*.json'))[-limit:]:
        try:
            value = read(path)
        except (OSError, ValueError):
            continue
        row = {'file': path.name, 'phase': value.get('phase'),
               'release': value.get('release'), 'status': value.get('status'),
               'started_at': value.get('started_at'),
               'total_seconds': value.get('total_seconds'),
               'slowest': value.get('slowest')}
        backup = value.get('notes', {}).get('backup')
        if isinstance(backup, dict):
            row['backup'] = {'total_seconds': backup.get('total_seconds'),
                             'archive_bytes': backup.get('archive_bytes'),
                             'uploads': backup.get('uploads')}
        rows.append(row)
    return rows


def timing_receipt(archive):
    """Locate the backup receipt that ``backup.sh`` writes next to the archive.

    ``backup.sh`` names it after the run id, not the encrypted file:
    ``powersystem-YYYYMMDDTHHMMSSZ.tar.gz.gpg`` →
    ``powersystem-YYYYMMDDTHHMMSSZ.timing.json``. The suffixed form is still
    accepted for backups produced by older tooling.
    """
    archive = Path(archive)
    stem = archive.with_name(archive.name[:-len('.tar.gz.gpg')]) \
        if archive.name.endswith('.tar.gz.gpg') else archive
    for candidate in (Path(str(stem) + '.timing.json'),
                      Path(str(archive) + '.timing.json')):
        if candidate.is_file():
            return candidate
    return Path(str(stem) + '.timing.json')


def backup_summary(archive):
    """Backup stages recorded by backup.sh next to the archive it produced."""
    path = timing_receipt(archive)
    if not path.is_file():
        return None
    try:
        value = read(path)
    except (OSError, ValueError):
        return None
    return {'file': path.name, 'status': value.get('status'),
            'total_seconds': value.get('total_seconds'),
            'archive_bytes': value.get('archive_bytes'),
            'staging_bytes': value.get('staging_bytes'),
            'slowest': value.get('slowest'), 'uploads': value.get('uploads'),
            'spans': value.get('spans')}


def verified_full_baseline(archive):
    """A recent full backup completed on both remote targets, not a local-only dump."""
    archive = Path(archive).resolve()
    require(archive.parent == BACKUP_DIR and
            bool(re.fullmatch(r'powersystem-\d{8}T\d{6}Z\.tar\.gz\.gpg', archive.name)),
            'fast_baseline_not_full_backup')
    require(archive.is_file(), 'fast_baseline_missing')
    receipt = timing_receipt(archive)
    require(receipt.is_file(), 'fast_baseline_receipt_missing')
    value = read(receipt)
    require(value.get('status') == 'ok' and value.get('archive') == archive.name and
            {row.get('name') for row in value.get('uploads', []) if isinstance(row, dict)} ==
            {'upload_primary', 'upload_secondary'}, 'fast_baseline_remote_copies_unproven')
    try:
        snapshot_started = datetime.datetime.fromisoformat(
            value['started_at'].replace('Z', '+00:00'))
        require(snapshot_started.tzinfo is not None, 'fast_baseline_time_invalid')
    except (KeyError, TypeError, ValueError):
        raise Blocked('fast_baseline_time_invalid') from None
    age = time.time() - snapshot_started.timestamp()
    require(0 <= age <= MAX_FAST_BASELINE_AGE, 'fast_baseline_too_old')
    expected = Path(str(archive) + '.sha256').read_text().split()[0]
    actual = sha(archive)
    require(actual == expected, 'fast_baseline_checksum_mismatch')
    return {'archive': archive.name, 'sha256': actual,
            'snapshot_started_at': snapshot_started.isoformat(),
            'created_at': datetime.datetime.fromtimestamp(
                archive.stat().st_mtime, datetime.timezone.utc).isoformat()}


def activation_time(pointer, path=None):
    """When the running application release was activated, ignoring re-signs.

    ``updated_at`` moves on every acceptance-pointer write, and a post-backup
    maintenance requalification is one of them. A full baseline has to follow the
    application release, not that re-sign, so ``published_at`` records the
    activation itself. Pointers written before the field existed are traced back
    through the retained ``backup-maintenance-*.json`` records to the pointer each
    re-sign replaced.
    """
    for _ in range(16):
        if pointer.get('published_at'):
            return datetime.datetime.fromisoformat(pointer['published_at'])
        for record in (sorted(Path(path).glob('backup-maintenance-*.json'), reverse=True)
                       if path is not None else ()):
            try:
                value = read(record)
            except (OSError, ValueError):
                continue
            if value.get('gate_file') == pointer.get('gate_file') and value.get('previous_pointer'):
                pointer = value['previous_pointer']
                break
        else:
            require(bool(pointer.get('updated_at')), 'activation_time_unknown')
            return datetime.datetime.fromisoformat(pointer['updated_at'])
    raise Blocked('backup_maintenance_chain_too_long')


def choose_fast_baseline(pointer):
    """A full snapshot after the last release activation, or its inherited baseline.

    The boundary is the activation time, never a maintenance re-sign: the re-sign
    happens after the backup it repairs, so using it would disqualify that same
    backup for a release that changed nothing but the frontend image.
    """
    boundary = activation_time(
        pointer, release(pointer['release']) if pointer.get('release') else None).timestamp()
    candidates = sorted((path for path in BACKUP_DIR.glob('powersystem-*.tar.gz.gpg')
                         if re.fullmatch(r'powersystem-\d{8}T\d{6}Z\.tar\.gz\.gpg',
                                         path.name)),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for archive in candidates:
        if archive.stat().st_mtime >= boundary:
            try:
                baseline = verified_full_baseline(archive)
                if datetime.datetime.fromisoformat(
                        baseline['snapshot_started_at']).timestamp() >= boundary:
                    return baseline
            except (Blocked, OSError, ValueError):
                continue
    inherited = pointer.get('backup_baseline')
    if inherited:
        return verified_full_baseline(BACKUP_DIR / inherited)
    raise Blocked('fast_baseline_must_follow_current_release')


def verified_fast_capsule(archive, manifest, pointer):
    """A release-specific encrypted frontend image/config bundle on both targets."""
    require(manifest.get('controller_sha256') == sha(__file__),
            'fast_capsule_controller_changed')
    archive = Path(archive).resolve()
    require(archive.parent == BACKUP_DIR and
            bool(re.fullmatch(r'powersystem-frontend-\d{8}T\d{6}Z\.tar\.gz\.gpg',
                              archive.name)), 'fast_capsule_path_invalid')
    require(archive.is_file(), 'fast_capsule_missing')
    receipt_path = Path(str(archive) + '.receipt.json')
    require(receipt_path.is_file(), 'fast_capsule_receipt_missing')
    receipt = read(receipt_path)
    require(receipt.get('status') == 'ok' and receipt.get('archive') == archive.name and
            receipt.get('release') == manifest['release'] and
            receipt.get('parent') == manifest['parent'] and
            receipt.get('manifest_sha256') == digest(manifest) and
            receipt.get('frontend_image') == manifest['images']['frontend'] and
            receipt.get('uploads') == ['primary', 'secondary'],
            'fast_capsule_identity_mismatch')
    require(sha(archive) == receipt.get('sha256'), 'fast_capsule_checksum_mismatch')
    require(archive.stat().st_mtime >=
            datetime.datetime.fromisoformat(manifest['created_at']).timestamp(),
            'fast_capsule_before_candidate')
    baseline = choose_fast_baseline(pointer)
    require(receipt.get('baseline') == baseline, 'fast_capsule_baseline_changed')
    return receipt, baseline


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


def chunk_names(names):
    """Logical frontend chunk names, independent of each build's content hash."""
    return {CHUNK.sub('', name) for name in names if name.endswith(('.js', '.css'))}


def frontend_chunk_names(image):
    output = command(['docker', 'run', '--rm', '--entrypoint', 'sh', image, '-c',
                      'ls /usr/share/nginx/html/assets'])
    return chunk_names(output.decode().split())


def missing_chunks(candidate, baseline):
    """Chunks the serving image has that the candidate image no longer ships."""
    return sorted(frontend_chunk_names(baseline) - frontend_chunk_names(candidate))


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


def validate_v4_readonly(services):
    """V4 isolation profiles never become production permission by publishing.

    This gate is deliberately independent of OPS_WRITE_ENABLED: schedule
    configuration and the new action authorities have separate switches.
    Enabling them needs an explicitly reviewed production profile first.
    """
    readonly = {'V4_ASSISTANT_ENABLED', 'V4_RUNS_ENABLED', 'V4_ARTIFACTS_ENABLED'}
    for service, spec in services.items():
        env = spec.get('environment', {})
        protocol = env.get('V4_READ_DELEGATION_PROTOCOL')
        if 'V4_READ_DELEGATION_PROTOCOL' in env:
            require(service == 'agent-rag' and protocol == 'ma/v1-bound-read',
                    'v4_production_read_protocol_not_authorized')
        for key in (key for key in env if key.startswith('V4_') and key not in readonly):
            if key == 'V4_READ_DELEGATION_PROTOCOL':
                continue  # Explicit old-production R0 bridge, never a write authority.
            # Docker compose config resolves env_file into this mapping.
            # Fail closed for unknown values and preserve absent old configs.
            require(key not in env or str(env[key]).lower() in ('false', '0'),
                    'v4_production_profile_not_authorized:' + service + ':' + key)
        if service == 'security-agent':
            require(env.get('SEC_RUNTIME_PROFILE') == 'm4-server-readonly',
                    'v4_production_security_profile_not_authorized')


def validate(config):
    require(config.get('name') == PROJECT, 'wrong_project')
    services = config['services']
    validate_v4_readonly(services)
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


def executable(fragment):
    """Text PostgreSQL still runs in a fragment after `--` comments are stripped."""
    output, quoted, comment, index = [], False, False, 0
    while index < len(fragment):
        char = fragment[index]
        if comment:
            if char == '\n':
                comment = False
                output.append(' ')
        elif quoted:
            output.append(char)
            if char == "'":
                quoted = False
        elif char == "'":
            quoted = True
            output.append(char)
        elif fragment.startswith('--', index):
            comment = True
            index += 1
        else:
            output.append(char)
        index += 1
    return ''.join(output).strip()


def migration_problems(name, content):
    """Mirror store.Migrate: split on every ';' and expect a statement in each part."""
    problems = []
    for number, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith('--') and ';' in stripped:
            problems.append(name + ':' + str(number) + ':semicolon_in_comment')
    if '$$' in content:
        problems.append(name + ':dollar_quote_unsupported')
    for fragment in content.split(';'):
        text = executable(fragment)
        if not text:
            continue
        head = re.split(r'[\s(]', text, maxsplit=1)[0].upper()
        if head not in MIGRATION_KEYWORDS:
            problems.append(name + ':unsupported_statement:' + head[:32])
    return problems


def migration_bundle(image):
    """Migration files exactly as the API image will read them at startup."""
    try:
        created = command(['docker', 'create', image])
    except Blocked:
        raise Blocked('preflight:migration_bundle_unavailable') from None
    container = created.decode(errors='replace').strip().splitlines()[-1]
    try:
        try:
            payload = command(['docker', 'cp', container + ':' + MIGRATION_DIR, '-'])
        except Blocked:
            raise Blocked('preflight:migration_bundle_missing') from None
    finally:
        subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=120)
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        return {Path(member.name).name: archive.extractfile(member).read().decode('utf-8')
                for member in archive.getmembers()
                if member.isfile() and member.name.endswith('.sql')}


def check_private_layout(config, envfile):
    """The frozen helper derives its target from the env file; prove that path exists."""
    env = Path(envfile)
    require(env.is_absolute() and env.is_file(), 'preflight:private_env_missing')
    require(env.parent.name == 'private', 'preflight:private_env_outside_batch_private')
    batch = env.parent.parent
    require(batch.is_relative_to(ROOT / 'runtime'), 'preflight:private_batch_outside_runtime')
    executor = batch / 'private/executor'
    require(executor.is_dir(), 'preflight:private_executor_directory_missing')
    require((executor / 'executor.json').is_file(), 'preflight:executor_config_missing')
    mounts = [mount for mount in config['services']['ops-executor'].get('volumes', [])
              if mount.get('target') == EXECUTOR_TARGET]
    require(len(mounts) == 1, 'preflight:executor_mount_missing')
    source = Path(mounts[0]['source'])
    require(source.is_dir(), 'preflight:executor_mount_source_missing')
    require(source.resolve() == executor.resolve(), 'preflight:executor_mount_outside_batch')
    return {'batch': batch.name, 'mounts': 1}


def check_asset_ledger(config, ledger=LEDGER):
    """A host ledger more than one version ahead means an identity consumed a version twice."""
    ledger = Path(ledger)
    require(ledger.is_file(), 'preflight:asset_registry_missing')
    try:
        registry = read(ledger)
    except (OSError, ValueError):
        raise Blocked('preflight:asset_registry_invalid') from None
    assets = registry.get('assets')
    identities = registry.get('private_assets')
    require(isinstance(assets, list) and bool(assets), 'preflight:asset_registry_empty')
    require(isinstance(identities, dict), 'preflight:asset_identities_missing')
    try:
        committed = json.loads(command([
            'docker', 'exec', PROJECT + '-postgres-1', 'psql', '-U', 'powersystem',
            '-d', 'ma_ops', '-Atqc',
            "SELECT coalesce(json_object_agg(asset_id,version),'{}'::json)::text FROM ma_assets"]))
    except (Blocked, ValueError):
        raise Blocked('preflight:asset_ledger_unreadable') from None
    require(isinstance(committed, dict), 'preflight:asset_ledger_unreadable')
    containers = 0
    for asset in assets:
        if not isinstance(asset, dict) or asset.get('kind') != 'container':
            continue
        containers += 1
        identifier = asset.get('asset_id')
        require(isinstance(identifier, str) and bool(identifier), 'preflight:asset_id_invalid')
        version = asset.get('version')
        require(isinstance(version, int) and not isinstance(version, bool),
                'preflight:asset_version_invalid:' + identifier)
        require(identifier in identities, 'preflight:asset_identity_missing:' + identifier)
        expected = committed.get(identifier, 0)
        require(isinstance(expected, int) and not isinstance(expected, bool),
                'preflight:asset_ledger_unreadable')
        # One step ahead is an uncommitted import; further means the version jumped.
        require(version <= expected + 1, 'preflight:asset_version_ahead_of_database:' + identifier)
    require(containers > 0, 'preflight:asset_registry_has_no_container')
    return {'assets': len(assets), 'containers': containers, 'committed': len(committed)}


def check_migrations(config):
    image = config['services']['api']['image']
    require(bool(ID.fullmatch(str(image))), 'preflight:api_image_not_pinned')
    files = migration_bundle(image)
    require(bool(files), 'preflight:migration_bundle_empty')
    problems = []
    for name in sorted(files):
        problems.extend(migration_problems(name, files[name]))
    if problems:
        raise Blocked('preflight:migration_split:' + problems[0])
    require(any('telemetry_inbox' in text for text in files.values()),
            'preflight:migration_entry_table_missing')
    return {'image': image, 'files': len(files),
            'statements': sum(len([part for part in text.split(';') if executable(part)])
                              for text in files.values())}


def preflight(config, envfile):
    """Short read-only checks that must fail before a backup is produced."""
    checks = []
    for name, action in (
        ('preflight.private_layout', lambda: check_private_layout(config, envfile)),
        ('preflight.asset_ledger', lambda: check_asset_ledger(config)),
        ('preflight.migration_entry', lambda: check_migrations(config)),
    ):
        with track(name) as span:
            span.update(action())
        checks.append({'check': name, 'seconds': span['seconds']})
    if TIMELINE is not None:
        TIMELINE.notes['preflight'] = checks
    return checks


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


def validate_fast_frontend_config(config, args, env, parent, services, overrides):
    require(parent is not None and services == {'frontend'} and
            set(overrides) == {'frontend'}, 'fast_tier_frontend_image_only')
    previous = release(parent)
    old = read(previous / 'private/compose.json')
    expected = read(previous / 'private/origin-args.json')
    require(args == expected and env == read(previous / 'manifest.json')['origin_environment'],
            'fast_tier_origin_changed')
    old['services']['frontend']['image'] = config['services']['frontend']['image']
    require(escaped(config) == old, 'fast_tier_configuration_changed')


def freeze(name, args_path, parent, services, overrides, backup_tier=FULL_TIER,
           allow_asset_shrink=False):
    with track('freeze.origin_args') as span:
        args, env = origin_args(args_path)
        span['arguments'] = len(args)
    with track('freeze.inspect_running'):
        rows = inspect()
        healthy(rows)
    with track('freeze.compose_config') as span:
        config = json.loads(command(args + ['config', '--format', 'json']))
        validate(config)
        span['services'] = len(config['services'])
    require(set(rows) == set(config['services']) - {'backup-helper'}, 'project_service_set_changed')
    with track('freeze.pin_anonymous_volumes'):
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
    with track('freeze.preserve_storage'):
        preserve_storage_and_ports(config, rows, set(rows))
    with track('freeze.check_unrelated_environment'):
        for service, row in rows.items():
            if service in services:
                continue
            actual_env = dict(item.split('=', 1) for item in row['Config']['Env'])
            require(all(actual_env.get(k) == str(v) for k, v in
                        config['services'][service].get('environment', {}).items()),
                    'undeclared_environment_change:' + service)
    with track('freeze.pin_images') as span:
        for service, value in config['services'].items():
            if service in rows:
                image = rows[service]['Image']
            else:
                image = json.loads(command(['docker', 'image', 'inspect', value['image']]))[0]['Id']
            if service in overrides:
                image = json.loads(command(['docker', 'image', 'inspect',
                                            overrides[service]]))[0]['Id']
            require(bool(ID.fullmatch(image)), 'image_id_required')
            value['image'] = image
            value.pop('build', None)
            value.pop('pull_policy', None)
        span['images'] = len(config['services'])
    if 'frontend' in overrides:
        # A build from a stale source tree silently drops route chunks. Compare the
        # logical asset names with the serving image before any candidate is written.
        with track('freeze.frontend_chunks') as span:
            missing = missing_chunks(config['services']['frontend']['image'],
                                     rows['frontend']['Image'])
            span['missing'] = missing
            require(not missing or allow_asset_shrink,
                    'frontend_assets_missing:' + ','.join(missing))
    # Fail here rather than after a fresh 650 MB backup and two remote uploads.
    preflight(config, env)
    if backup_tier == FAST_TIER:
        validate_fast_frontend_config(config, args, env, parent, services, overrides)
        choose_fast_baseline(read(STATE_DIR / 'active.json'))
    else:
        require(backup_tier == FULL_TIER, 'unknown_backup_tier')
    path = release(name)
    require(not path.exists(), 'release_already_exists')
    with track('freeze.write_candidate'):
        path.mkdir(parents=True, mode=0o700)
        write(path / 'private/compose.json', escaped(config))
        write(path / 'private/origin-args.json', args)
        for source, target in [
            (ROOT / 'deploy/edge-server/独立云服务器部署/scripts/check-auth-wss.py', path / 'gate.py'),
            (ROOT / 'stages/09-ops-agent/deploy/ma-release.py', path / 'registry-helper.py'),
        ]:
            shutil.copyfile(source, target)
            target.chmod(0o600)
        files = ['private/compose.json', 'private/origin-args.json', 'gate.py', 'registry-helper.py']
        m = {'manifest_version': 1, 'release': name, 'created_at': now(), 'parent': parent,
             'backup_tier': backup_tier,
             'controller_sha256': sha(__file__),
             'services': sorted(services), 'origin_environment': env,
             'images': {s: v['image'] for s, v in config['services'].items()},
             'files': {n: sha(path / n) for n in files},
             'private_references': references(config, env), 'before': states(rows)}
        write(path / 'manifest.json', m)
    with track('freeze.roundtrip'):
        # Docker parses the frozen JSON but no container or HTTP check is run here.
        expanded = json.loads(command(compose(path) + ['config', '--format', 'json']))
        require(expanded == config, 'frozen_compose_roundtrip_mismatch')
    return path, m


def set_active(path, gate_name, backup_baseline=None, published_at=None):
    """Move the acceptance pointer; a maintenance re-sign keeps the activation time."""
    m = read(path / 'manifest.json')
    published = published_at or now()
    if not isinstance(published, str):
        published = published.isoformat()
    pointer = {'release': path.name, 'manifest_sha256': digest(m),
               'gate_file': gate_name, 'updated_at': now(), 'published_at': published}
    if backup_baseline is not None:
        pointer['backup_baseline'] = backup_baseline
    write(path / 'acceptance-pointer.json', pointer)
    write(STATE_DIR / 'active.json', pointer)


def current_matches(path, m):
    gate = read(path / read(STATE_DIR / 'active.json')['gate_file'])
    require(states(inspect()) == gate['states'], 'running_baseline_drift')


def maintenance_states_match(before, after):
    """A full backup may restart only its two writers, never recreate them."""
    require(set(before) == set(after), 'backup_maintenance_service_set_changed')
    for service, expected in before.items():
        actual = after[service]
        keys = ('id', 'image', 'restarts') if service in ('agent-rag', 'ops-agent') else expected.keys()
        require(all(actual.get(k) == expected[k] for k in keys),
                'backup_maintenance_identity_changed:' + service)


def backup_maintenance_snapshot(path, after=False, archive=None):
    """Called under backup.sh's existing lock; no deployment or pointer update."""
    path = Path(path)
    if archive is not None:
        value = read(path)
        require('after' in value, 'backup_maintenance_resume_unproven')
        value.update(archive=Path(archive).name, sha256=sha(archive))
        write(Path(str(archive) + '.v4-maintenance.json'), value)
        return
    release_path, manifest = active()
    rows = inspect()
    healthy(rows)
    if not after:
        current_matches(release_path, manifest)
        write(path, {'version': 1, 'active': read(STATE_DIR / 'active.json'),
                     'before': states(rows), 'started_at': now()})
    else:
        value = read(path)
        require(value['active'] == read(STATE_DIR / 'active.json'),
                'backup_maintenance_active_changed')
        maintenance_states_match(value['before'], states(rows))
        value.update(after=states(rows), resumed_at=now())
        write(path, value)


def verify_backup_maintenance(a):
    """Requalify this same release after its recorded backup pause/resume only."""
    path, manifest = active()
    pointer = read(STATE_DIR / 'active.json')
    baseline = verified_full_baseline(a.backup)
    receipt = read(Path(str(Path(a.backup).resolve()) + '.v4-maintenance.json'))
    require(receipt.get('version') == 1 and receipt.get('active') == pointer and
            receipt.get('archive') == baseline['archive'] and
            receipt.get('sha256') == baseline['sha256'], 'backup_maintenance_receipt_mismatch')
    old_gate = read(path / pointer['gate_file'])
    require(receipt['before'] == old_gate['states'], 'backup_maintenance_baseline_mismatch')
    maintenance_states_match(receipt['before'], receipt['after'])
    rows = inspect()
    healthy(rows)
    require(states(rows) == receipt['after'], 'backup_maintenance_post_resume_drift')
    require(bool(manifest.get('parent')) and bool(manifest['services']),
            'backup_maintenance_parent_required')
    transaction = {'previous': manifest['parent'], 'candidate': path.name,
                   'services': manifest['services'], 'before': states(rows),
                   'started_at': now(), 'kind': 'post_backup_maintenance',
                   'backup': baseline}
    write(STATE_DIR / 'pending.json', transaction)
    try:
        accepted_gate = gate(path, [], states(rows))
        # Record the old receipt and gate; only the existing gate can issue the
        # new acceptance pointer. Every original gate/manifest stays retained.
        write(path / ('backup-maintenance-' + str(time.time_ns()) + '.json'),
              {'previous_pointer': pointer, 'receipt': receipt, 'gate_file': accepted_gate})
        set_active(path, accepted_gate, pointer.get('backup_baseline'),
                   pointer.get('published_at') or activation_time(pointer, path).isoformat())
        (STATE_DIR / 'pending.json').unlink()
    except Exception:
        restore_transaction(transaction)
        raise


def rollback_maintenance_matches(receipt_path, path, pointer, rows):
    """A failed backup may prove resume, but cannot issue new acceptance."""
    receipt_path = Path(receipt_path).resolve()
    require(receipt_path.parent == BACKUP_DIR and
            re.fullmatch(r'powersystem-\d{8}T\d{6}Z\.tar\.gz\.gpg\.v4-maintenance.json', receipt_path.name),
            'backup_maintenance_receipt_path_invalid')
    receipt = read(receipt_path)
    require(receipt.get('version') == 1 and receipt.get('active') == pointer,
            'backup_maintenance_receipt_mismatch')
    resumed = datetime.datetime.fromisoformat(receipt['resumed_at'])
    require(resumed.tzinfo is not None and 0 <= time.time() - resumed.timestamp() <= 86400,
            'backup_maintenance_receipt_expired')
    require(receipt['before'] == read(path / pointer['gate_file'])['states'],
            'backup_maintenance_baseline_mismatch')
    maintenance_states_match(receipt['before'], receipt['after'])
    healthy(rows)
    require(states(rows) == receipt['after'], 'backup_maintenance_post_resume_drift')


def maintenance_receipt(archive, m, pm, archive_sha):
    """The pause/resume record of the full backup this candidate was planned on.

    A full backup stops and restarts the two V4 writers, so the state after it can
    never equal the snapshot frozen before it. This receipt is the only accepted
    explanation for that difference: same parent release and frozen manifest, the
    same encrypted archive proven on both targets, the candidate's own freeze
    snapshot as ``before``, and nothing but the two writers' start times allowed to
    move. It never replaces an acceptance gate, never rewrites the frozen manifest
    and excuses no other drift.
    """
    archive = Path(archive).resolve()
    path = Path(str(archive) + '.v4-maintenance.json')
    if not path.is_file():
        return None
    receipt = read(path)
    baseline = verified_full_baseline(archive)
    require(baseline['sha256'] == archive_sha, 'backup_maintenance_archive_changed')
    require(receipt.get('version') == 1 and 'before' in receipt and 'after' in receipt,
            'backup_maintenance_receipt_incomplete')
    require(receipt.get('archive') == archive.name and receipt.get('sha256') == archive_sha,
            'backup_maintenance_receipt_archive_mismatch')
    pointer = receipt.get('active') or {}
    require(pointer.get('release') == m['parent'] and
            pointer.get('manifest_sha256') == digest(pm) and
            bool(pointer.get('gate_file')), 'backup_maintenance_parent_mismatch')
    require(read(release(m['parent']) / pointer['gate_file']).get('states') == receipt['before'],
            'backup_maintenance_baseline_mismatch')
    maintenance_states_match(receipt['before'], receipt['after'])
    require(receipt['before'] == m['before'], 'backup_maintenance_candidate_mismatch')
    try:
        started = datetime.datetime.fromisoformat(receipt['started_at'])
        resumed = datetime.datetime.fromisoformat(receipt['resumed_at'])
        frozen = datetime.datetime.fromisoformat(m['created_at'])
    except (KeyError, TypeError, ValueError):
        raise Blocked('backup_maintenance_receipt_incomplete') from None
    require(started.timestamp() >= frozen.timestamp(),
            'backup_maintenance_receipt_predates_candidate')
    require(resumed.timestamp() >= started.timestamp(), 'backup_maintenance_receipt_incomplete')
    return receipt


def require_baseline(running, snapshot, receipt, reason):
    """The running state is the accepted snapshot, or its own proven backup window."""
    if running == snapshot:
        return 'snapshot'
    require(receipt is not None and receipt['before'] == snapshot and
            running == receipt['after'], reason)
    return 'backup_maintenance'


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
    with track('prepare.resolve_active'):
        previous, pm = active()
    with track('prepare.confirm_running_baseline'):
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
    freeze(a.id, args_path, previous.name, services, overrides, a.tier,
           getattr(a, 'allow_asset_shrink', False))


def drain():
    count = command(['docker', 'exec', PROJECT + '-postgres-1', 'psql', '-U', 'powersystem',
                     '-d', 'ma_ops', '-Atqc', "SELECT count(*) FROM ma_tasks WHERE status IN "
                     "('queued','planning','executing','awaiting_approval')"]).decode().strip()
    require(count == '0', 'inflight_tasks_block_release')


def refresh_registry(path, envfile):
    with track('deploy.registry_snapshot') as span:
        before = states(inspect())['ops-executor']
        registry = LEDGER
        ids_before = {v['asset_id'] for v in read(registry)['assets']}
        span['assets'] = len(ids_before)
    with track('deploy.registry_helper_load'):
        spec = importlib.util.spec_from_file_location('frozen_registry_helper',
                                                      path / 'registry-helper.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module.write

        def redirected(destination, value):
            if (Path(destination).name == 'asset-refresh.json' or
                    Path(destination).name.startswith('asset-import-')):
                destination = path / 'events' / (str(time.time_ns()) + '-' +
                                                 Path(destination).name)
                Path(destination).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            return original(destination, value)
        module.write = redirected
    with track('deploy.registry_refresh') as span:
        # Includes the Executor reload; it restarts the container on every release.
        module.refresh_assets(Path(envfile).parent.parent)
        span['executor_reload'] = True
    with track('deploy.registry_verify'):
        after = states(inspect())['ops-executor']
        require(all(after[k] == before[k] for k in ('id', 'image', 'restarts')),
                'executor_identity_changed')
        require({v['asset_id'] for v in read(registry)['assets']} == ids_before,
                'new_asset_forbidden')


def started_at(row):
    return datetime.datetime.strptime(row['State']['StartedAt'][:19],
                                      '%Y-%m-%dT%H:%M:%S').replace(
        tzinfo=datetime.timezone.utc).timestamp()


def api_startup_wait_diagnostic(diagnostic):
    for line in diagnostic.splitlines():
        line = line.strip()
        if (line == 'timed out waiting for containers to become healthy' or
                line == 'container ' + PROJECT + '-api-1 is unhealthy' or
                re.fullmatch(r'application not healthy after [0-9hms.]+', line)):
            return True
    return False


def wait_initial_api(manifest, services, seconds=480):
    """Allow a live, unchanged API to finish its large latest-value scan.

    This is startup time only. No unhealthy instance is accepted, and every
    original gate still runs after this function returns.
    """
    require('api' in services, 'startup_wait_requires_api')
    initial = inspect()
    require(set(services).issubset(initial), 'startup_service_missing')
    before = states({name: initial[name] for name in services})
    deadline = time.monotonic() + seconds
    while True:
        rows = inspect()
        require(set(services).issubset(rows), 'startup_service_missing')
        require(states({name: rows[name] for name in services}) == before,
                'startup_instance_changed')
        for name in services:
            row = rows[name]
            require(row['Image'] == manifest['images'][name] and
                    row['State']['Running'] and not row['State']['OOMKilled'] and
                    row['RestartCount'] == 0, 'startup_instance_not_stable')
            if name != 'api':
                healthy({name: row})
        status = rows['api']['State'].get('Health', {}).get('Status')
        require(status in ('starting', 'unhealthy', 'healthy'), 'api_health_probe_required')
        if status == 'healthy':
            healthy({name: rows[name] for name in services})
            return
        require(time.monotonic() < deadline, 'api_initialization_timeout')
        time.sleep(2)


def healthy_parent_already_deployed(path, services):
    """An interrupted rollback can finish its gate without another restart."""
    m = immutable(path)
    config = read(path / 'private/compose.json')
    rows = inspect()
    try:
        healthy(rows)
    except Blocked:
        return False
    for name in services:
        if name not in rows:
            return False
        row = rows[name]
        labels = row['Config']['Labels']
        env = dict(value.split('=', 1) for value in row['Config']['Env'])
        if (row['Image'] != m['images'][name] or
                row['State']['OOMKilled'] or row['RestartCount'] != 0 or
                labels.get('com.docker.compose.project') != PROJECT or
                labels.get('com.docker.compose.service') != name or
                labels.get('com.docker.compose.project.config_files') != str(path / 'private/compose.json') or
                labels.get('com.docker.compose.project.environment_file') != m['origin_environment'] or
                not all(env.get(key) == str(value) for key, value in
                        config['services'][name].get('environment', {}).items())):
            return False
    return True


def deploy(path, services):
    with track('deploy.immutable'):
        m = immutable(path)
    log = path / ('up-' + str(time.time_ns()) + '.log')
    wall = time.time()
    with track('deploy.compose_up') as span:
        span['services'] = len(services)
        try:
            command(compose(path) + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                                    '--force-recreate', '--wait', '--wait-timeout', '120', *services],
                    log)
        except Blocked as error:
            diagnostic = log.read_text(errors='replace') if log.is_file() else ''
            known_wait = api_startup_wait_diagnostic(diagnostic)
            if str(error) != 'command_failed:docker' or not known_wait:
                raise
            wait_initial_api(m, services)
            span['api_initialization_grace_used'] = True
        span['log_bytes'] = log.stat().st_size if log.is_file() else 0
    with track('deploy.nginx'):
        if {'api', 'frontend'} & set(services):
            command(['docker', 'exec', PROJECT + '-frontend-1', 'nginx', '-t'])
            command(['docker', 'exec', PROJECT + '-frontend-1', 'nginx', '-s', 'reload'])
    refresh_registry(path, m['origin_environment'])
    with track('deploy.readiness') as span:
        rows = inspect()
        span['started'] = {service: round(max(0.0, started_at(rows[service]) - wall), 1)
                           for service in sorted(services) if service in rows}


def gate(path, services, before):
    with track('gate.immutable'):
        m = immutable(path)
    with track('gate.inspect'):
        rows = inspect()
        healthy(rows)
    require(set(rows) == set(before), 'service_set_changed')
    with track('gate.identity') as span:
        span['services'] = len(rows)
        for service, row in rows.items():
            require(row['Image'] == m['images'][service], 'unexpected_image:' + service)
            if service not in services:
                expected = before[service]
                actual = states({service: row})[service]
                keys = ('id', 'image', 'restarts') if service == 'ops-executor' else expected.keys()
                require(all(actual[k] == expected[k] for k in keys),
                        'unrelated_service_changed:' + service)
    with track('gate.compose_config'):
        config = json.loads(command(compose(path) + ['config', '--format', 'json']))
        validate(config)
    with track('gate.environment_match'):
        for service in rows:
            actual = dict(v.split('=', 1) for v in rows[service]['Config']['Env'])
            require(all(actual.get(k) == str(v) for k, v in
                        config['services'][service].get('environment', {}).items()),
                    'runtime_environment_mismatch:' + service)
    with track('gate.https_wss') as span:
        # Per-entry timings come from gate.py when that frozen copy supports them.
        evidence = path / ('gate-timing-' + str(time.time_ns()) + '.json')
        command([sys.executable, str(path / 'gate.py'), '/etc/powersystem/demo-password'],
                path / ('https-wss-' + str(time.time_ns()) + '.log'),
                env=dict(os.environ, POWERSYSTEM_GATE_TIMING=str(evidence)))
        if evidence.is_file():
            try:
                value = read(evidence)
                span['hosts'] = value.get('hosts')
                span['total_seconds_reported'] = value.get('total_seconds')
            except (OSError, ValueError):
                span['hosts'] = None
    with track('gate.final_inspect'):
        final_rows = inspect()
        healthy(final_rows)
        require(states(final_rows) == states(rows), 'runtime_changed_during_acceptance')
    with track('gate.persist'):
        immutable(path)
        name = 'accepted-' + str(time.time_ns()) + '.json'
        write(path / name, {'passed': True, 'manifest_sha256': digest(m), 'states': states(rows),
                            'kind': 'automatic_https_wss_gate', 'accepted_at': now()})
    return name


def restore_transaction(transaction):
    previous = release(transaction['previous'])
    with track('rollback.verify_previous'):
        previous_pointer = read(previous / 'acceptance-pointer.json')
        accepted(previous, previous_pointer)
    services = transaction['services']
    with track('rollback.drain'):
        drain()
    with track('rollback.deploy'):
        if healthy_parent_already_deployed(previous, services):
            pm = immutable(previous)
            refresh_registry(previous, pm['origin_environment'])
            if {'api', 'frontend'} & set(services):
                command(['docker', 'exec', PROJECT + '-frontend-1', 'nginx', '-t'])
                command(['docker', 'exec', PROJECT + '-frontend-1', 'nginx', '-s', 'reload'])
        else:
            deploy(previous, services)
    with track('rollback.gate'):
        gate_name = gate(previous, services, transaction['before'])
    with track('rollback.activate'):
        set_active(previous, gate_name, previous_pointer.get('backup_baseline'))
        write(STATE_DIR / ('rollback-' + str(time.time_ns()) + '.json'),
              {'passed': True, 'transaction': transaction, 'restored_at': now()})
        (STATE_DIR / 'pending.json').unlink()


def publish(a):
    with track('publish.resolve_active'):
        previous, pm = active()
    with track('publish.candidate_manifest'):
        candidate = release(a.id)
        m = immutable(candidate)
    require(m['parent'] == previous.name, 'stale_or_unrelated_candidate')
    services = m['services']
    require(bool(services) and set(services).issubset(APPLICATIONS), 'invalid_release_services')
    archive = Path(a.backup).resolve()
    tier = m.get('backup_tier', FULL_TIER)
    require(tier in (FULL_TIER, FAST_TIER), 'unknown_backup_tier')
    baseline = None
    maintenance = None
    if tier == FAST_TIER:
        with track('publish.verify_fast_capsule') as span:
            receipt, baseline = verified_fast_capsule(
                archive, m, read(STATE_DIR / 'active.json'))
            archive_sha = receipt['sha256']
            span['bytes'] = archive.stat().st_size
    else:
        require(archive.parent == BACKUP_DIR and
                bool(re.fullmatch(r'powersystem-\d{8}T\d{6}Z\.tar\.gz\.gpg', archive.name)),
                'owned_encrypted_backup_required')
        with track('publish.verify_backup') as span:
            span['bytes'] = archive.stat().st_size
            archive_sha = sha(archive)
            require(archive_sha == Path(str(archive) + '.sha256').read_text().split()[0],
                    'backup_checksum_mismatch')
        with track('publish.backup_ordering'):
            age = datetime.datetime.now().timestamp() - archive.stat().st_mtime
            require(0 <= age <= 86400, 'recent_predeployment_backup_required')
            require(archive.stat().st_mtime >=
                    datetime.datetime.fromisoformat(m['created_at']).timestamp(),
                    'backup_must_follow_candidate_preparation')
        # That backup paused and resumed the two writers after this manifest was
        # frozen; only its own receipt can explain the difference, and both baseline
        # checks below plus the full gate still have to pass.
        with track('publish.maintenance_receipt'):
            maintenance = maintenance_receipt(archive, m, pm, archive_sha)
    with track('publish.confirm_baseline'):
        running = states(inspect())
        checkpoint = read(STATE_DIR / 'active.json')
        require_baseline(running, read(previous / checkpoint['gate_file'])['states'],
                         maintenance, 'running_baseline_drift')
    with track('publish.confirm_health'):
        healthy(inspect())
    with track('publish.baseline_drift'):
        require_baseline(running, m['before'], maintenance, 'candidate_baseline_drift')
    if TIMELINE is not None:
        TIMELINE.notes['candidate_created_at'] = m['created_at']
        TIMELINE.notes['prepare_to_publish_seconds'] = round(
            time.time() - datetime.datetime.fromisoformat(m['created_at']).timestamp(), 3)
        TIMELINE.notes['backup'] = backup_summary(archive)
        TIMELINE.notes['backup_tier'] = tier
        if baseline:
            TIMELINE.notes['fast_baseline'] = baseline
    # The checks that used to be discovered by a failing startup, run again here so a
    # prepared candidate cannot bypass them; they are cheap compared with the release.
    preflight(read(candidate / 'private/compose.json'), m['origin_environment'])
    with track('publish.drain'):
        drain()
    with track('publish.transaction_write'):
        transaction = {'previous': previous.name, 'candidate': candidate.name,
                       'services': services, 'before': states(inspect()), 'started_at': now(),
                       'predeployment_backup': archive.name, 'backup_sha256': archive_sha,
                       'backup_tier': tier, 'backup_baseline': baseline}
        write(STATE_DIR / 'pending.json', transaction)
    try:
        with track('publish.deploy'):
            deploy(candidate, services)
        with track('publish.gate'):
            accepted = gate(candidate, services, transaction['before'])
        with track('publish.activate'):
            set_active(candidate, accepted, baseline['archive'] if baseline else None)
            (STATE_DIR / 'pending.json').unlink()
    except Exception as error:
        with track('publish.record_failure'):
            write(candidate / ('failed-' + str(time.time_ns()) + '.json'),
                  {'passed': False, 'error_type': type(error).__name__,
                   'reason': str(error) if isinstance(error, Blocked) else None, 'at': now()})
        # Keep pending.json if rollback itself fails; subsequent releases stay blocked.
        restore_transaction(transaction)
        raise Blocked('release_failed_and_previous_release_restored') from None


def verify_candidate(a):
    path = release(a.id)
    m = immutable(path)
    preflight(read(path / 'private/compose.json'), m['origin_environment'])


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
    plan.add_argument('--tier', choices=(FULL_TIER, FAST_TIER), default=FULL_TIER)
    plan.add_argument('--allow-asset-shrink', action='store_true')
    check = sub.add_parser('preflight')
    check.add_argument('--id', required=True)
    activate = sub.add_parser('publish')
    activate.add_argument('--id', required=True)
    activate.add_argument('--backup', required=True)
    maintenance = sub.add_parser('verify-backup')
    maintenance.add_argument('--backup', required=True)
    rollback = sub.add_parser('rollback')
    rollback.add_argument('--backup-maintenance')
    sub.add_parser('status')
    watch = sub.add_parser('timing')
    watch.add_argument('--limit', type=int, default=10)
    a = parser.parse_args()
    require(socket.gethostname() == 'R730XD' and os.geteuid() == 0, 'specified_root_business_host_required')
    os.umask(0o077)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if a.phase == 'timing':
        print(json.dumps({'timings': recent_timings(max(1, a.limit))}))
        return
    global TIMELINE
    TIMELINE = Timeline(a.phase, getattr(a, 'id', None))
    import fcntl
    try:
        with (STATE_DIR / 'deployment.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            pending = STATE_DIR / 'pending.json'
            if a.phase == 'status':
                pointer = (read(STATE_DIR / 'active.json')
                           if (STATE_DIR / 'active.json').exists() else None)
                print(json.dumps({'active': pointer, 'pending': pending.exists()}))
                return
            if a.phase == 'rollback':
                if not pending.exists():
                    current, cm = active()
                    if a.backup_maintenance:
                        rollback_maintenance_matches(a.backup_maintenance, current,
                                                     read(STATE_DIR / 'active.json'), inspect())
                    else:
                        current_matches(current, cm)
                    require(bool(cm['parent']) and bool(cm['services']), 'no_previous_release')
                    accepted(release(cm['parent']),
                             read(release(cm['parent']) / 'acceptance-pointer.json'))
                    write(pending, {'previous': cm['parent'], 'candidate': current.name,
                                    'services': cm['services'], 'before': states(inspect()),
                                    'started_at': now(), 'kind': 'explicit_parent_rollback'})
                restore_transaction(read(pending))
            else:
                require(not pending.exists(), 'unfinished_release_requires_rollback')
                {'prepare': prepare, 'bootstrap': bootstrap, 'preflight': verify_candidate,
                 'verify-backup': verify_backup_maintenance,
                 'publish': publish}[a.phase](a)
    except Exception as error:
        persist_timing('failed', error)
        raise
    record, value = persist_timing('ok')
    print(json.dumps({'phase': a.phase, 'completed': True,
                      'verification_run': a.phase in ('publish', 'rollback', 'verify-backup'),
                      'total_seconds': value['total_seconds'] if value else None,
                      'timing_file': str(record) if record else None,
                      'slowest': value['slowest'] if value else None}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Avoid leaking Compose output, credentials, JWTs or private configuration.
        reason = str(error) if isinstance(error, Blocked) else type(error).__name__
        print(json.dumps({'completed': False, 'blocked': reason}), file=sys.stderr)
        raise SystemExit(1)
