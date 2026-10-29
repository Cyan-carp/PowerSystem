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
import signal
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
                'gateway', 'ai-predict', 'prometheus', 'alertmanager'}
ID = re.compile(r'sha256:[a-f0-9]{64}')
CHUNK = re.compile(r'-[A-Za-z0-9_-]{6,12}\.(?:js|css)$')
NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
BACKUP_DIR = Path('/var/backups/powersystem')
FAST_TIER = 'frontend-fast'
STANDARD_TIER = 'standard-fast'
FULL_TIER = 'full'
STANDARD_SERVICES = {'api', 'gateway', 'frontend', 'agent-rag', 'ops-agent', 'security-agent'}
MAX_FAST_BASELINE_AGE = 86400
# One-off, owner-authorized deferral of the pre-deployment full backup. Only these
# two explicitly named candidates may carry it, and only at the full tier. This is
# not a general --skip-backup/--force switch: no other release can be named here.
DEFERRAL_CANDIDATES = ('v4-production-compat-20261026-r1', 'v4-business-schedule-20261026-r1',
                       'v4-business-schedule-20261026-r2')
DEFERRAL_SERVICES = ('agent-rag', 'api', 'ops-agent', 'security-agent')
DEFERRAL_FILE = 'backup-deferral.json'


class Blocked(RuntimeError):
    pass


def registered_business_host(capability):
    helper = Path(__file__).with_name('host-profile.py')
    require(helper.is_file(), 'host_profile_helper_required')
    spec = importlib.util.spec_from_file_location('powersystem_host_profile', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.require_registered_host(capability)
    except (module.HostError, OSError, ValueError, KeyError) as error:
        raise Blocked(str(error)) from None


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


def telemetry_protocol():
    helper = Path(__file__).with_name('telemetry-maintenance.py')
    require(helper.is_file(), 'telemetry_maintenance_helper_required')
    spec = importlib.util.spec_from_file_location('powersystem_telemetry_maintenance', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def telemetry_context():
    from types import SimpleNamespace
    return SimpleNamespace(command=command, inspect=inspect, healthy=healthy, states=states,
                           read=read, write=write, STATE_DIR=STATE_DIR, BACKUP_DIR=BACKUP_DIR,
                           ROOT=ROOT, PROJECT=PROJECT, require=require, compose=compose, sha=sha, now=now,
                           telemetry_protocol=telemetry_protocol, telemetry_context=telemetry_context,
                           telemetry_monitoring=telemetry_monitoring, release=release, Blocked=Blocked)


def telemetry_transition():
    helper = Path(__file__).with_name('telemetry-transition.py')
    require(helper.is_file(), 'telemetry_transition_helper_required')
    spec = importlib.util.spec_from_file_location('powersystem_telemetry_transition', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def telemetry_monitoring():
    helper = Path(__file__).with_name('telemetry-monitoring.py')
    require(helper.is_file(), 'telemetry_monitoring_helper_required')
    spec = importlib.util.spec_from_file_location('powersystem_telemetry_monitoring', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def telemetry_repair():
    helper = Path(__file__).with_name('telemetry-repair.py')
    spec = importlib.util.spec_from_file_location('powersystem_telemetry_repair', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def telemetry_repair_context():
    # Backup imports this module without registering its temporary module name.
    from types import SimpleNamespace
    return SimpleNamespace(**globals())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
    os.replace(temp, path)


CANCEL_SAFE_COMMANDS = False


def cancel_safe_run(args, **kwargs):
    timeout = kwargs.pop('timeout')
    kwargs.pop('capture_output')
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True, **kwargs) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except BaseException:
            # Compose is a CLI/plugin process group. Prove that group has exited
            # before inspecting and stopping its newly created containers.
            try:os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:pass
            try:child.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                try:os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:pass
                child.communicate(timeout=5)
            raise
        return subprocess.CompletedProcess(args,child.returncode,stdout,stderr)


def command(args, log=None, env=None, timeout=600):
    try:
        runner=cancel_safe_run if CANCEL_SAFE_COMMANDS else subprocess.run
        result = runner(args, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as error:
        if log:
            Path(log).write_bytes((error.stdout or b'')+(error.stderr or b''))
            Path(log).chmod(0o600)
        raise Blocked('command_timeout:'+Path(args[0]).name) from None
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


def standard_assessment(source, name, parent, images):
    """Bind the compatible tier to actual tested images and retained test evidence."""
    document = read(owned(source))
    require(document.get('format') == 1 and document.get('kind') == 'compatible-release/v1' and
            document.get('candidate') == name and document.get('parent') == parent and
            document.get('tested_images') == images, 'standard_assessment_binding_mismatch')
    require(document.get('rpo_seconds') == MAX_FAST_BASELINE_AGE and
            document.get('rollback_compatible') is True and
            document.get('production_config_matched') is True,
            'standard_compatibility_and_rpo_required')
    for field in ('schema_changed', 'data_repair', 'persistent_semantics_changed', 'permissions_changed',
                  'storage_changed', 'contracts_changed'):
        require(document.get(field) is False, 'standard_high_risk_change:' + field)
    tests = document.get('tests')
    require(isinstance(tests, list) and bool(tests), 'standard_targeted_tests_required')
    evidence = []
    for test in tests:
        require(isinstance(test, dict) and isinstance(test.get('name'), str) and
                bool(test['name'].strip()) and type(test.get('exit_code')) is int and
                test['exit_code'] == 0, 'standard_targeted_test_failed')
        path = owned(test['evidence'])
        require(path.is_relative_to(ROOT/'runtime') or path.is_relative_to(ROOT/'artifacts'),
                'standard_evidence_outside_runtime')
        require(path.stat().st_size > 0 and sha(path) == test.get('sha256'),
                'standard_evidence_changed')
        evidence.append(path)
    return document, evidence


def protected_source_files(files, service):
    roots = {'api': ('02-backend',), 'gateway': ('02-backend',),
             'frontend': ('04-frontend',), 'agent-rag': ('08-agent-rag',),
             'ops-agent': ('08-agent-rag', '09-ops-agent', '10-security-agent'),
             'security-agent': ('10-security-agent',)}[service]
    protected = {}
    for name, record in files.items():
        parts = Path(name).parts
        if len(parts) < 3 or parts[1] not in roots:
            continue
        lower = name.lower()
        if (lower.endswith('.sql') or any(word in lower for word in
                ('migrat', 'schema', 'contract', 'permission', 'policy')) or
                Path(name).name in {'Dockerfile', 'go.mod', 'go.sum', 'package.json',
                                    'package-lock.json', 'requirements.txt', 'pyproject.toml',
                                    'uv.lock', 'poetry.lock'}):
            protected[name] = record
    return protected


def validate_standard_config(config, args, env, parent, services, overrides, source_builds):
    require(parent is not None and bool(services) and services <= STANDARD_SERVICES and
            set(overrides) == services, 'standard_image_changes_only')
    previous = release(parent)
    old = read(previous/'private/compose.json')
    require(args == read(previous/'private/origin-args.json') and
            env == read(previous/'manifest.json')['origin_environment'],
            'standard_origin_changed')
    receipts = {item['service']: item for item in source_builds}
    store = ROOT/'runtime/source-builds'
    for service in sorted(services):
        before = old['services'][service]['image']
        after = config['services'][service]['image']
        require(before != after and service in receipts, 'standard_new_receipt_required:' + service)
        prior_path = store/'receipts'/(before[7:]+'.json')
        require(prior_path.is_file(), 'standard_parent_source_required:' + service)
        prior = read(prior_path)
        require(prior.get('image') == before and prior.get('service') == service and
                bool(re.fullmatch(r'[a-f0-9]{64}', prior.get('source_sha256', ''))),
                'standard_parent_source_required:' + service)
        metadata = json.loads(command(['docker', 'image', 'inspect', before]))[0]
        labels = metadata.get('Config', {}).get('Labels') or {}
        require(metadata.get('Id') == before and
                labels.get('io.powersystem.source.sha256') == prior['source_sha256'] and
                labels.get('io.powersystem.source.service') == service,
                'standard_parent_image_source_mismatch:' + service)
        original = read(store/'contexts'/prior['source_sha256']/'source-manifest.json')
        current = read(store/'contexts'/receipts[service]['source_sha256']/'source-manifest.json')
        require(digest(original) == prior['source_sha256'] and
                digest(current) == receipts[service]['source_sha256'], 'standard_source_changed')
        require(protected_source_files(original['files'], service) ==
                protected_source_files(current['files'], service),
                'standard_protected_source_changed:' + service)
        old['services'][service]['image'] = after
    require(escaped(config) == old, 'standard_configuration_changed')


def verified_standard_capsule(archive, manifest, pointer):
    require(manifest.get('backup_tier') == STANDARD_TIER and
            manifest.get('controller_sha256') == sha(__file__), 'standard_controller_changed')
    archive = Path(archive).resolve()
    require(archive.parent == BACKUP_DIR and bool(re.fullmatch(
        r'powersystem-release-\d{8}T\d{6}Z\.tar\.gz\.gpg', archive.name)), 'standard_capsule_path_invalid')
    receipt = read(Path(str(archive)+'.receipt.json'))
    images = {s: manifest['images'][s] for s in manifest['services']}
    require(receipt.get('status') == 'ok' and receipt.get('archive') == archive.name and
            receipt.get('release') == manifest['release'] and
            receipt.get('parent') == manifest['parent'] and
            receipt.get('manifest_sha256') == digest(manifest) and
            receipt.get('images') == images and receipt.get('uploads') == ['primary', 'secondary'],
            'standard_capsule_identity_mismatch')
    require(sha(archive) == receipt.get('sha256'), 'standard_capsule_checksum_mismatch')
    require(archive.stat().st_mtime >= datetime.datetime.fromisoformat(
        manifest['created_at']).timestamp(), 'standard_capsule_before_candidate')
    # Keep the exact baseline selected by the capsule, even when a newer scheduled
    # backup finishes meanwhile. Validate its eligibility and age again.
    baseline = verified_full_baseline(BACKUP_DIR/receipt['baseline']['archive'])
    boundary = activation_time(pointer, release(pointer['release'])).timestamp()
    require(datetime.datetime.fromisoformat(baseline['snapshot_started_at']).timestamp() >= boundary or
            pointer.get('backup_baseline') == baseline['archive'], 'standard_baseline_incompatible')
    require(receipt['baseline'] == baseline, 'standard_capsule_baseline_changed')
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


def read_backup_deferral(source, name, parent, tier, services):
    """The owner's written authorization to publish these two candidates without
    a fresh full backup. Every field is explicit; nothing is inferred from a flag,
    an environment variable or a previous release.
    """
    require(tier == FULL_TIER and name in DEFERRAL_CANDIDATES,
            'backup_deferral_not_authorized_for_candidate')
    path = owned(source)
    try:
        doc = read(path)
    except (OSError, ValueError):
        raise Blocked('backup_deferral_invalid_json') from None
    require(isinstance(doc, dict) and doc.get('schema') == 1 and
            doc.get('kind') == 'backup_deferral_authorization',
            'backup_deferral_schema_required')
    require(doc.get('candidate') == name and doc.get('parent') == parent,
            'backup_deferral_candidate_or_parent_mismatch')
    require(doc.get('owner_requested_defer_until_effects_accepted') is True,
            'backup_deferral_owner_request_required')
    require(sorted(doc.get('services') or []) == list(DEFERRAL_SERVICES),
            'backup_deferral_scope_mismatch')
    require(tuple(sorted(services)) == DEFERRAL_SERVICES,
            'backup_deferral_release_scope_mismatch')
    instruction = doc.get('owner_instruction')
    require(isinstance(instruction, str) and len(instruction.strip()) >= 20,
            'backup_deferral_owner_instruction_required')
    try:
        recorded = datetime.datetime.fromisoformat(
            str(doc.get('recorded_at')).replace('Z', '+00:00'))
    except ValueError:
        raise Blocked('backup_deferral_timestamp_invalid') from None
    require(recorded.tzinfo is not None and recorded.utcoffset() == datetime.timedelta(0),
            'backup_deferral_timestamp_invalid')
    require(recorded.timestamp() <= time.time(), 'backup_deferral_timestamp_in_future')
    return path, doc, sha(path)


def bind_backup_deferral(path, name, parent, source, doc):
    """Freeze the authorized bytes beside the candidate and digest them.

    The copy is byte-identical to what the owner wrote, is listed in the
    manifest ``files`` map, and is therefore covered by ``immutable``. Its digest
    is repeated in ``backup_deferral`` so the block cannot point at another file.
    """
    target = Path(path) / DEFERRAL_FILE
    shutil.copyfile(source, target)
    target.chmod(0o600)
    value = sha(target)
    # Carry the owner's real boolean into the manifest: publish re-checks it, so the
    # block must not be assembled later from a constant the document never supplied.
    accepted = doc.get('owner_requested_defer_until_effects_accepted')
    require(accepted is True, 'backup_deferral_owner_request_required')
    return {'file': DEFERRAL_FILE, 'sha256': value, 'candidate': name, 'parent': parent,
            'recorded_at': doc['recorded_at'],
            'owner_requested_defer_until_effects_accepted': accepted,
            'owner_instruction_sha256': hashlib.sha256(
                doc['owner_instruction'].encode()).hexdigest()}


def frozen_backup_deferral(path, m, parent):
    """Re-verify the authorization prepare froze into this candidate.

    The document the owner signed is not re-read from its original location: the
    frozen copy is byte-identical, listed in ``files`` and covered by the pointer's
    manifest digest, so neither the document nor its binding can change after the
    freeze. Passing ``--backup`` is unaffected and remains the stricter path.
    """
    block = m.get('backup_deferral')
    require(isinstance(block, dict) and block.get('file') == DEFERRAL_FILE,
            'predeployment_backup_required')
    require(m.get('backup_tier', FULL_TIER) == FULL_TIER and
            m['release'] in DEFERRAL_CANDIDATES and
            block.get('candidate') == m['release'] and block.get('parent') == parent and
            block.get('owner_requested_defer_until_effects_accepted') is True and
            tuple(sorted(m.get('services') or [])) == DEFERRAL_SERVICES,
            'backup_deferral_frozen_binding_mismatch')
    _, doc, value = read_backup_deferral(path / DEFERRAL_FILE, m['release'], parent,
                                         FULL_TIER, list(m['services']))
    require(value == block.get('sha256') == m['files'].get(DEFERRAL_FILE),
            'backup_deferral_frozen_file_changed')
    require(block.get('recorded_at') == doc['recorded_at'] and
            block.get('owner_instruction_sha256') ==
            hashlib.sha256(doc['owner_instruction'].encode()).hexdigest(),
            'backup_deferral_frozen_binding_mismatch')
    return {'file': DEFERRAL_FILE, 'sha256': value, 'recorded_at': doc['recorded_at'],
            'owner_instruction': doc['owner_instruction']}


def inspect(allow_empty=False):
    ids = command(['docker', 'ps', '-aq', '--filter',
                   'label=com.docker.compose.project=' + PROJECT]).decode().split()
    if allow_empty and not ids:
        return {}
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


def inspect_empty_allowed():
    return inspect(allow_empty=True)


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


def check_source_builds(config, rows, services):
    """New V4 application images need a receipt bound to complete build inputs.

    Existing image IDs, accepted history and rollback remain usable unchanged.
    """
    guarded = {'api', 'gateway', 'frontend', 'agent-rag', 'ops-agent', 'security-agent'}
    changed = sorted(s for s in set(services) & guarded
                     if config['services'][s]['image'] != rows[s]['Image'])
    if not changed:
        return []
    helper = Path(__file__).with_name('source-build.py')
    require(helper.is_file(), 'source_build_helper_missing')
    spec = importlib.util.spec_from_file_location('powersystem_source_build', helper)
    source_build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source_build)
    results = []
    for service in changed:
        image = config['services'][service]['image']
        metadata = json.loads(command(['docker', 'image', 'inspect', image]))[0]
        try:
            results.append(source_build.verify_receipt(
                image, service, ROOT / 'runtime/source-builds', metadata))
        except (source_build.SourceError, OSError, ValueError, KeyError) as error:
            raise Blocked('source_build_required:'+service+':'+str(error)) from None
    return results


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
    configured = {name: spec.get('environment', {}).get('V4_PRODUCTION_PROFILE')
                  for name, spec in services.items()
                  if spec.get('environment', {}).get('V4_PRODUCTION_PROFILE')}
    if configured:
        require(set(configured) == {'api', 'agent-rag', 'ops-agent', 'security-agent'}
                and set(configured.values()) == {'v4-business-schedule/v1'},
                'v4_production_profile_incomplete_or_unknown')
        validate_v4_production(services)
        return
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


def validate_v4_production(services):
    """One explicit business/schedule profile; never a broad V4 switch bypass."""
    profile = 'v4-business-schedule/v1'
    common = {'V4_PRODUCTION_PROFILE': profile}
    action = {**common, 'V4_ACTIONS_ENABLED': 'true', 'V4_SCHEDULES_ENABLED': 'true',
              'V4_OPS_WRITE_ENABLED': 'false'}
    expected = {'api': action, 'ops-agent': action, 'security-agent': common,
                'agent-rag': {**action, 'V4_ASSISTANT_ENABLED': 'true',
                              'V4_RUNS_ENABLED': 'true', 'V4_ARTIFACTS_ENABLED': 'true',
                              'V4_READ_DELEGATION_PROTOCOL': 'ma-run/v1'}}
    host = registered_business_host('release')
    roots = set()
    for name, spec in services.items():
        env = spec.get('environment', {})
        actual = {k: str(v) for k, v in env.items() if k.startswith('V4_')}
        require(actual == expected.get(name, {}), 'v4_production_capability_matrix:' + name)
        if name not in expected:
            continue
        mounts = [v for v in spec.get('volumes', []) if v.get('target') == '/run/v4-production']
        require(len(mounts) == 1 and mounts[0].get('type') == 'bind' and mounts[0].get('read_only') is True,
                'v4_production_identity_readonly_mount:' + name)
        root = Path(mounts[0]['source']).resolve()
        require(root.is_relative_to(ROOT / 'runtime') and root.parent.name == 'private',
                'v4_production_identity_outside_private')
        roots.add(root)
    require(len(roots) == 1, 'v4_production_identity_must_match_across_services')
    root = roots.pop()
    doc = read(root / 'identity.json')
    require(set(doc) == {'profile', 'host', 'databases', 'origins', 'capabilities', 'policies'}
            and doc['profile'] == profile
            and doc['host'] == {'hostname': 'powersystem-prod', 'vm_id': 117,
                                'machine_id_sha256': host.get('machine_id_sha256'),
                                'dmi_uuid': host.get('dmi_uuid')}
            and host.get('hostname') == 'powersystem-prod'
            and sha(root / 'machine-id') == host.get('machine_id_sha256')
            and (root / 'product-uuid').read_text().strip().lower() == host.get('dmi_uuid')
            and doc['databases'] == {'business': 'powersystem', 'rag': 'powersystem_agent_rag', 'ops': 'ma_ops'}
            and doc['origins'] == {'api': 'http://api:8080', 'rag': 'http://agent-rag:8093',
                                  'ops': 'http://ops-agent:8094', 'security': 'http://security-agent:8100'}
            and doc['capabilities'] == {'actions': True, 'schedules': True, 'ops_write': False}
            and doc['policies'] == {'business': 'v4-s06-production/v1', 'schedule': 'v4-s08-production/v1'},
            'v4_production_machine_topology_or_policy_mismatch')
    ops = services['ops-agent'].get('environment', {})
    sec = services['security-agent'].get('environment', {})
    rag = services['agent-rag'].get('environment', {})
    require(ops.get('OPS_RUNTIME_PROFILE') == 'm4-server-readonly'
            and ops.get('OPS_WRITE_ENABLED') == 'false'
            and ops.get('OPS_INFRA_ENABLED') == 'true'
            and ops.get('OPS_SECURITY_ENABLED') == 'true'
            and ops.get('OPS_INSPECTION_ENABLED') == 'true'
            and sec.get('SEC_RUNTIME_PROFILE') == 'm4-server-readonly',
            'v4_production_legacy_readonly_domain_required')
    require(sec.get('SEC_ACTION_LLM_URL') == 'https://api.deepseek.com'
            and sec.get('SEC_ACTION_MODEL') == 'deepseek-flash'
            and sec.get('SEC_ACTION_LLM_API_KEY_FILE', '').startswith('/run/')
            and sec.get('SEC_ACTION_LLM_API_KEY_FILE') != sec.get('SEC_LLM_API_KEY_FILE'),
            'v4_production_independent_official_review_required')
    require(str(rag.get('RAG_TOOL_TIMEOUT', '')).isdigit()
            and int(rag['RAG_TOOL_TIMEOUT']) >= 40,
            'v4_production_action_review_budget_required')
    bridge_files = [services[n].get('environment', {}).get('SCHEDULE_BRIDGE_TOKEN_FILE')
                    for n in ('api', 'agent-rag')]
    require(bridge_files[0] == bridge_files[1] and bridge_files[0]
            and bridge_files[0].startswith('/run/'), 'v4_production_independent_bridge_required')


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
    deferral = m.get('backup_deferral')
    if deferral is not None:
        require(deferral.get('file') == DEFERRAL_FILE and
                deferral.get('sha256') == m['files'].get(DEFERRAL_FILE),
                'frozen_backup_deferral_unbound')
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
           allow_asset_shrink=False, defer_backup=None, telemetry_evidence=None, frozen_repair=None,
           assessment=None):
    defer_source = defer_doc = None
    require(not set(services) & {'prometheus', 'alertmanager'} or telemetry_evidence is not None,
            'monitoring_change_requires_telemetry_plan')
    if defer_backup:
        # Fail before any Docker work when the authorization is not usable.
        defer_source, defer_doc, _ = read_backup_deferral(
            defer_backup, name, parent, backup_tier, sorted(services))
    with track('freeze.origin_args') as span:
        args, env = origin_args(args_path)
        span['arguments'] = len(args)
    with track('freeze.inspect_running'):
        rows = inspect()
        if frozen_repair:
            require(name == 'tr-s04-20261028-r4', 'telemetry_repair_candidate_invalid')
            telemetry_repair().admit(telemetry_repair_context(), frozen_repair)
        else:
            healthy(rows)
    with track('freeze.compose_config') as span:
        config = json.loads(command(args + ['config', '--format', 'json']))
        validate(config)
        span['services'] = len(config['services'])
    require(set(rows) == set(config['services']) - {'backup-helper'}, 'project_service_set_changed')
    telemetry_monitoring().inherit(telemetry_context(), parent, config, rows, services)
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
    with track('freeze.source_builds') as span:
        source_builds = check_source_builds(config, rows, services)
        span['verified'] = len(source_builds)
    transition_document = None
    transition = telemetry_transition()
    if telemetry_evidence is not None:
        require(defer_backup is None, 'telemetry_transition_requires_new_full_backup')
        transition_document = transition.validate(telemetry_context(), read(owned(telemetry_evidence)),
                                                  parent, services, backup_tier, source_builds, name)
    transition.require_if_needed(telemetry_context(), source_builds, transition_document)
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
    assessment_doc = assessment_files = None
    if backup_tier == STANDARD_TIER:
        require(assessment is not None, 'standard_assessment_required')
        validate_standard_config(config, args, env, parent, services, overrides, source_builds)
        assessment_doc, assessment_files = standard_assessment(
            assessment, name, parent, {s: config['services'][s]['image'] for s in services})
        choose_fast_baseline(read(STATE_DIR/'active.json'))
    elif backup_tier == FAST_TIER:
        validate_fast_frontend_config(config, args, env, parent, services, overrides)
        choose_fast_baseline(read(STATE_DIR / 'active.json'))
    else:
        require(backup_tier == FULL_TIER, 'unknown_backup_tier')
    path = release(name)
    require(not path.exists(), 'release_already_exists')
    with track('freeze.write_candidate'):
        path.mkdir(parents=True, mode=0o700)
        monitoring_files = (telemetry_monitoring().prepare(telemetry_context(), path, transition_document,
                                                          config, rows) if transition_document else [])
        write(path / 'private/compose.json', escaped(config))
        write(path / 'private/origin-args.json', args)
        for source, target in [
            (ROOT / 'deploy/edge-server/独立云服务器部署/scripts/check-auth-wss.py', path / 'gate.py'),
            (ROOT / 'stages/09-ops-agent/deploy/ma-release.py', path / 'registry-helper.py'),
        ]:
            shutil.copyfile(source, target)
            target.chmod(0o600)
        files = ['private/compose.json', 'private/origin-args.json', 'gate.py', 'registry-helper.py']+monitoring_files
        if assessment_doc is not None:
            write(path/'compatible-assessment.json', assessment_doc)
            files.append('compatible-assessment.json')
            for index, evidence in enumerate(assessment_files):
                relative = 'evidence/test-' + str(index) + '.log'
                (path/'evidence').mkdir(exist_ok=True, mode=0o700)
                shutil.copyfile(evidence, path/relative)
                files.append(relative)
        if defer_source is not None:
            files.append(DEFERRAL_FILE)
            deferred = bind_backup_deferral(path, name, parent, defer_source, defer_doc)
        if transition_document is not None:
            write(path/'telemetry-transition.json', transition_document)
            files.append('telemetry-transition.json')
        if frozen_repair:
            shutil.copyfile(owned(frozen_repair), path/'telemetry-repair.json')
            files.append('telemetry-repair.json')
        m = {'manifest_version': 1, 'release': name, 'created_at': now(), 'parent': parent,
             'backup_tier': backup_tier,
             'controller_sha256': sha(__file__),
             'services': sorted(services), 'origin_environment': env,
             'images': {s: v['image'] for s, v in config['services'].items()},
             'source_builds': source_builds,
             'files': {n: sha(path / n) for n in files},
             'private_references': references(config, env), 'before': states(rows)}
        if defer_source is not None:
            m['backup_deferral'] = deferred
        if transition_document is not None:
            m['telemetry_transition'] = {'sha256': sha(path/'telemetry-transition.json')}
        if frozen_repair:
            m['telemetry_repair'] = {'sha256': sha(path/'telemetry-repair.json')}
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


def maintenance_states_match(before, after, telemetry=None):
    """A full backup may restart only its two writers, never recreate them."""
    require(set(before) == set(after), 'backup_maintenance_service_set_changed')
    gateway_maintained = False
    if telemetry is not None:
        protocol = telemetry_protocol()
        try:
            gateway_maintained = protocol.validate_receipt(before, after, telemetry)
        except protocol.TelemetryBlocked as error:
            raise Blocked(str(error)) from None
        except (KeyError, TypeError, ValueError):
            raise Blocked('telemetry_backup_receipt_invalid') from None
    for service, expected in before.items():
        actual = after[service]
        keys = ('id', 'image', 'restarts') if (service in ('agent-rag', 'ops-agent') or
                                            service == 'gateway' and gateway_maintained) else expected.keys()
        require(all(actual.get(k) == expected[k] for k in keys),
                'backup_maintenance_identity_changed:' + service)


def failed_backup_resume_chain(release_path, rows, evidence_path):
    """Bind a failed pre-collection pause to a subsequent real full backup.

    This issues no acceptance. The next full backup, publish and complete gate
    remain mandatory. A retained candidate supplies the observed original state;
    the failed timing and live identities constrain the two intervening restarts.
    """
    proof_path=owned(evidence_path)
    require(proof_path.is_relative_to(ROOT/'runtime'), 'failed_backup_evidence_scope')
    proof=read(proof_path)
    require(proof.get('format')=='failed-backup-resume-chain/v1', 'failed_backup_evidence_format')
    pointer=read(STATE_DIR/'active.json')
    candidate=release(proof['candidate']); frozen=immutable(candidate)
    gate_before=read(release_path/pointer['gate_file'])['states']
    require(frozen['parent']==release_path.name and frozen['before']==gate_before,
            'failed_backup_candidate_baseline_mismatch')
    if proof.get('receipt'):
        receipt_path=Path(proof['receipt']).resolve()
        rollback_maintenance_matches(receipt_path,release_path,pointer,rows)
        receipt=read(receipt_path)
        require(receipt.get('archive') is None and not Path(str(receipt_path).removesuffix('.v4-maintenance.json')).exists(),
                'failed_backup_receipt_must_not_claim_archive')
        return {'before':gate_before,'observed_before':states(rows),'started_at':receipt['started_at'],
                'actual_backup_started_at':now(),'previous_failed_backup':{
                    'candidate':candidate.name,'candidate_manifest_sha256':sha(candidate/'manifest.json'),
                    'retained_resume_receipt':receipt,'receipt_sha256':sha(receipt_path),
                    'evidence_sha256':sha(proof_path),'acceptance_issued':False}}
    timing_path=Path(proof['timing']).resolve()
    require(timing_path.parent==BACKUP_DIR and re.fullmatch(r'powersystem-\d{8}T\d{6}Z\.timing.json',timing_path.name),
            'failed_backup_timing_scope')
    timing=read(timing_path)
    require(timing.get('status')=='failed' and timing.get('archive_bytes')==0 and not timing.get('uploads')
            and timing.get('failed_stage')=='v4_rows'
            and not (BACKUP_DIR/timing['archive']).exists(), 'failed_backup_not_precollection_failure')
    require(any(s.get('name')=='v4_pause' for s in timing.get('spans',[])), 'failed_backup_pause_unproven')
    start=datetime.datetime.fromisoformat(timing['started_at'].replace('Z','+00:00'))
    end=datetime.datetime.fromisoformat(timing['finished_at'].replace('Z','+00:00'))
    frozen_at=datetime.datetime.fromisoformat(frozen['created_at'])
    # backup.sh's timing started_at has whole-second precision; freeze has micros.
    require(start.tzinfo is not None and start.timestamp()+1>=frozen_at.timestamp()
            and 0<=time.time()-end.timestamp()<=86400, 'failed_backup_evidence_expired')
    current=states(rows)
    draft_path=Path(str(BACKUP_DIR/timing['archive'])+'.v4-maintenance.json')
    telemetry=None
    if draft_path.is_file():
        draft=read(draft_path)
        require(draft.get('active')==read(STATE_DIR/'active.json') and
                draft.get('before')==gate_before and draft.get('after')==current,
                'failed_backup_resume_draft_unbound')
        telemetry=draft.get('telemetry')
    maintenance_states_match(gate_before,current,telemetry)
    restarted_services=('agent-rag','ops-agent','gateway') if telemetry else ('agent-rag','ops-agent')
    for name in restarted_services:
        restarted=datetime.datetime.fromisoformat(current[name]['started_at'].replace('Z','+00:00'))
        require(start.timestamp()<=restarted.timestamp()<=end.timestamp()+5, 'failed_backup_restart_outside_window')
    return {'before':gate_before,'observed_before':current,'started_at':max(start,frozen_at).isoformat(),
            'actual_backup_started_at':now(),'previous_failed_backup':{
                'failure_timing_start_precision':'seconds',
                'candidate':candidate.name,'candidate_manifest_sha256':sha(candidate/'manifest.json'),
                'failure_timing':timing,'failure_timing_sha256':sha(timing_path),
                'evidence_sha256':sha(proof_path),'acceptance_issued':False}}


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
    if (STATE_DIR/'pending.json').exists():
        repair = telemetry_repair()
        if not after:
            proof_path, proof, rows = repair.backup_context(telemetry_repair_context())
            write(path, {'version':1,'active':read(STATE_DIR/'active.json'),'before':states(rows),
                'started_at':now(),'frozen_repair_sha256':sha(proof_path),'repair_candidate':proof['candidate']})
        else:
            value=read(path)
            repair.backup_context(telemetry_repair_context(),states_after=states(rows))
            maintenance_states_match(value['before'],states(rows))
            telemetry=telemetry_protocol().receipt(telemetry_context(),path.parent/'telemetry-backup-state.json')
            require(telemetry is not None and telemetry.get('kind')==repair.FORMAT and
                    telemetry.get('phase')=='preserved_frozen', 'telemetry_frozen_backup_unproven')
            value.update(after=states(rows),resumed_at=now(),telemetry=telemetry)
            write(path,value)
        return
    healthy(rows)
    if not after:
        observation={'before':states(rows),'started_at':now()}
        try:
            current_matches(release_path, manifest)
        except Blocked:
            proof=os.getenv('POWERSYSTEM_FAILED_BACKUP_RESUME_EVIDENCE')
            if not proof:raise
            observation=failed_backup_resume_chain(release_path,rows,proof)
        write(path, {'version': 1, 'active': read(STATE_DIR / 'active.json'),**observation})
    else:
        value = read(path)
        require(value['active'] == read(STATE_DIR / 'active.json'),
                'backup_maintenance_active_changed')
        protocol = telemetry_protocol()
        telemetry = protocol.receipt(telemetry_context(), path.parent/'telemetry-backup-state.json')
        if protocol.schema(telemetry_context(), rows)['s04']:
            require(telemetry is not None, 'telemetry_backup_protocol_required')
        maintenance_states_match(value['before'], states(rows), telemetry)
        if telemetry is not None:
            value['telemetry'] = telemetry
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
    maintenance_states_match(receipt['before'], receipt['after'], receipt.get('telemetry'))
    rows = inspect()
    healthy(rows)
    require(states(rows) == receipt['after'], 'backup_maintenance_post_resume_drift')
    if getattr(a, 'phase', None) == 'reconcile-backup':
        # Reconcile only a successful, precisely bound scheduled maintenance.
        # There is no application deployment and no new functional acceptance.
        accepted_gate = gate(path, [], states(rows), maintenance_only=True)
        write(path/('backup-maintenance-'+str(time.time_ns())+'.json'), {
            'previous_pointer': pointer, 'receipt': receipt, 'gate_file': accepted_gate,
            'inherited_gate_file': pointer['gate_file'], 'https_wss_rerun': False,
            'kind': 'successful_backup_state_reconciliation'})
        set_active(path, accepted_gate, pointer.get('backup_baseline'),
                   pointer.get('published_at') or activation_time(pointer, path).isoformat())
        return
    migrated = manifest.get('migration', {}).get('format') == 1
    if migrated:
        require(read(path / 'migration-baseline.json').get('passed') is True,
                'migration_baseline_required')
    require(migrated or (bool(manifest.get('parent')) and bool(manifest['services'])),
            'backup_maintenance_parent_required')
    transaction = {'previous': manifest['parent'] or path.name, 'candidate': path.name,
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
    maintenance_states_match(receipt['before'], receipt['after'], receipt.get('telemetry'))
    healthy(rows)
    require(states(rows) == receipt['after'], 'backup_maintenance_post_resume_drift')


def verify_failed_backup_resume(a):
    """Full gate after a proven failed-backup resume; never a backup success."""
    path, manifest = active()
    pointer = read(STATE_DIR/'active.json')
    rows = inspect()
    receipt_path = Path(a.receipt)
    require(receipt_path.is_file() and not receipt_path.is_symlink() and
            receipt_path.resolve().parent == BACKUP_DIR, 'backup_maintenance_receipt_path_invalid')
    receipt_path = receipt_path.resolve()
    rollback_maintenance_matches(receipt_path, path, pointer, rows)
    receipt = read(receipt_path)
    archive = Path(str(receipt_path).removesuffix('.v4-maintenance.json'))
    timing_path = Path(str(archive).removesuffix('.tar.gz.gpg')+'.timing.json')
    timing = read(timing_path)
    require(receipt.get('archive') is None and not archive.exists() and
            timing.get('status') == 'failed' and timing.get('archive_bytes') == 0 and
            not timing.get('uploads') and timing.get('failed_stage') in ('queues_snapshot', 'v4_rows'),
            'failed_backup_resume_not_proven')
    # No container is changed. A failing gate leaves the old pointer untouched.
    accepted_gate = gate(path, [], states(rows))
    write(path/('failed-backup-resume-'+str(time.time_ns())+'.json'), {
        'previous_pointer': pointer, 'receipt_sha256': sha(receipt_path),
        'timing_sha256': sha(timing_path), 'gate_file': accepted_gate,
        'backup_passed': False, 'kind': 'failed_backup_resume_requalification'})
    set_active(path, accepted_gate, pointer.get('backup_baseline'),
               pointer.get('published_at') or activation_time(pointer, path).isoformat())


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
    maintenance_states_match(receipt['before'], receipt['after'], receipt.get('telemetry'))
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
           getattr(a, 'allow_asset_shrink', False),
           getattr(a, 'defer_backup_authorization', None),
           getattr(a, 'telemetry_transition', None), assessment=getattr(a, 'assessment', None))


def drain():
    count = command(['docker', 'exec', PROJECT + '-postgres-1', 'psql', '-U', 'powersystem',
                     '-d', 'ma_ops', '-Atqc', "SELECT count(*) FROM ma_tasks WHERE status IN "
                     "('queued','planning','executing','awaiting_approval')"]).decode().strip()
    require(count == '0', 'inflight_tasks_block_release')
    # RAG has an independent database and queue. A protocol/profile transition
    # must not strand previously admitted runs even when the Ops queue is empty.
    args = ['docker', 'exec', PROJECT + '-postgres-1', 'psql', '-U', 'powersystem',
            '-d', 'powersystem_agent_rag', '-Atqc']
    exists = command(args + ["SELECT to_regclass('public.assistant_runs') IS NOT NULL"]).decode().strip()
    if exists == 't':
        count = command(args + ["SELECT count(*) FROM assistant_runs WHERE status IN "
                                "('queued','running','awaiting_approval')"]).decode().strip()
        require(count == '0', 'inflight_assistant_runs_block_release')


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


def deploy(path, services, startup_seconds=480):
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
            wait_initial_api(m, services, startup_seconds)
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


def gate(path, services, before, maintenance_only=False):
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
        gate_env = dict(os.environ, POWERSYSTEM_GATE_TIMING=str(evidence))
        # A frozen legacy gate ignores this variable and therefore keeps its
        # original full checks. No old candidate becomes weaker after an update.
        gate_env['POWERSYSTEM_GATE_PROFILE'] = ('standard' if
            m.get('backup_tier') == STANDARD_TIER else 'full')
        if not maintenance_only:
            command([sys.executable, str(path / 'gate.py'), '/etc/powersystem/demo-password'],
                    path / ('https-wss-' + str(time.time_ns()) + '.log'), env=gate_env)
        else:
            span['inherited_existing_acceptance'] = True
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
                            'kind': ('maintenance_state_reconciliation' if maintenance_only else
                                     'automatic_https_wss_gate'), 'accepted_at': now(),
                            'tests_rerun': not maintenance_only,
                            'profile': ('maintenance' if maintenance_only else
                                'standard' if m.get('backup_tier') == STANDARD_TIER else 'full')})
    return name


def restore_transaction(transaction):
    previous = release(transaction['previous'])
    with track('rollback.verify_previous'):
        previous_pointer = read(previous / 'acceptance-pointer.json')
        accepted(previous, previous_pointer)
    protocol = telemetry_protocol()
    transition = telemetry_transition()
    guarded = transition.needs_schema_guard(telemetry_context(), transaction)
    try:
        if guarded:
            try:
                transition.ensure_migrator_stopped(telemetry_context(), release(transaction['candidate']))
            except Blocked:
                protocol.halt_forward_recovery(telemetry_context(), inspect(), transaction,
                    {'state': 'unknown'}, 'telemetry_migrator_requires_manual_recovery')
            protocol.refuse_unsafe_parent(telemetry_context(), previous, transaction)
            transition.restore_pre_schema_gate(telemetry_context(), release(transaction['candidate']))
    except (protocol.TelemetryBlocked, Blocked) as error:
        if set(transaction.get('services', [])) & {'prometheus', 'alertmanager'}:
            # Monitoring can use its immutable parent configuration without ever
            # starting a historical telemetry worker against the forward schema.
            try:
                deploy(previous, ['prometheus', 'alertmanager'])
                write(STATE_DIR/'telemetry-monitoring-recovered.json', {
                    'passed': False, 'business_recovered': False, 'at': now(),
                    'monitoring_parent': previous.name, 'pending_retained': True})
            except Exception:
                write(STATE_DIR/'telemetry-monitoring-recovery-failed.json', {
                    'passed': False, 'at': now(), 'pending_retained': True})
        raise Blocked(str(error)) from None
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
    transition = telemetry_transition()
    transition_document = transition.frozen(telemetry_context(), candidate, m)
    transition.require_if_needed(telemetry_context(), m.get('source_builds', []), transition_document)
    if transition_document is not None:
        transition.before_cutover(telemetry_context())
    services = m['services']
    require(bool(services) and set(services).issubset(APPLICATIONS), 'invalid_release_services')
    archive = Path(a.backup).resolve() if a.backup else None
    tier = m.get('backup_tier', FULL_TIER)
    require(tier in (FULL_TIER, FAST_TIER, STANDARD_TIER), 'unknown_backup_tier')
    baseline = None
    maintenance = None
    deferral = None
    if archive is None:
        # No backup argument is accepted only for the owner-authorized candidates
        # whose frozen manifest carries the authorization; see frozen_backup_deferral.
        with track('publish.backup_deferral') as span:
            deferral = frozen_backup_deferral(candidate, m, previous.name)
            span['authorization_sha256'] = deferral['sha256']
        archive_sha = None
    elif tier == STANDARD_TIER:
        with track('publish.verify_standard_capsule') as span:
            receipt, baseline = verified_standard_capsule(archive, m, read(STATE_DIR/'active.json'))
            archive_sha = receipt['sha256']
            span['bytes'] = archive.stat().st_size
    elif tier == FAST_TIER:
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
        if archive is not None:
            TIMELINE.notes['backup'] = backup_summary(archive)
        TIMELINE.notes['backup_tier'] = tier
        if deferral:
            # Recorded as deferred, never as a passed backup check.
            TIMELINE.notes['backup_deferred'] = {
                'authorization_sha256': deferral['sha256'],
                'authorization_recorded_at': deferral['recorded_at']}
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
                       'backup_tier': tier, 'backup_baseline': baseline}
        if deferral:
            # Truthful: no pre-deployment backup exists for this release.
            transaction['backup_deferred'] = {
                'authorization_file': deferral['file'],
                'authorization_sha256': deferral['sha256'],
                'authorization_recorded_at': deferral['recorded_at'],
                'owner_instruction': deferral['owner_instruction'],
                'effects_accepted': False, 'recorded_at': now()}
        else:
            transaction['predeployment_backup'] = archive.name
            transaction['backup_sha256'] = archive_sha
        write(STATE_DIR / 'pending.json', transaction)
    try:
        if transition_document is not None:
            with track('publish.telemetry_schema'):
                transition.apply(telemetry_context(), candidate, m, transaction)
        with track('publish.deploy'):
            deploy(candidate, services)
        with track('publish.gate'):
            accepted = gate(candidate, services, transaction['before'])
        with track('publish.planned_silence_cleanup'):
            telemetry_monitoring().cleanup_silence(telemetry_context(), candidate)
        with track('publish.activate'):
            set_active(candidate, accepted, baseline['archive'] if baseline else None)
            (STATE_DIR / 'pending.json').unlink()
    except Exception as error:
        with track('publish.record_failure'):
            write(candidate / ('failed-' + str(time.time_ns()) + '.json'),
                  {'passed': False, 'error_type': type(error).__name__,
                   'reason': str(error) if isinstance(error, Blocked) else None, 'at': now()})
        # Keep pending.json if rollback itself fails; subsequent releases stay blocked.
        try:
            restore_transaction(transaction)
        finally:
            telemetry_monitoring().cleanup_silence(telemetry_context(), candidate, strict=False)
        raise Blocked('release_failed_and_previous_release_restored') from None


def verify_candidate(a):
    path = release(a.id)
    m = immutable(path)
    preflight(read(path / 'private/compose.json'), m['origin_environment'])


def resume_frozen_telemetry(a):
    """Resume the exact migrated candidate, preserving its failed transaction."""
    require(a.id in ('tr-s04-20261028-r3','tr-s04-20261028-r4') and a.attempt in ('r1', 'r2', 'r3'),
            'telemetry_resume_scope_invalid')
    candidate = release(a.id)
    m = immutable(candidate)
    pending = STATE_DIR/'pending.json'
    recovery = STATE_DIR/'telemetry-forward-recovery-required.json'
    evidence_path = owned(a.evidence)
    require(evidence_path.is_relative_to(ROOT/'runtime'), 'telemetry_resume_evidence_scope')
    proof = read(evidence_path)
    transaction = read(pending)
    previous, pm = active()
    require(proof.get('format') == 'telemetry-same-candidate-resume/v1' and
            proof.get('candidate') == a.id and proof.get('attempt') == a.attempt and
            proof.get('manifest_sha256') == digest(m) and proof.get('pending_sha256') == sha(pending) and
            proof.get('recovery_sha256') == sha(recovery) and
            proof.get('active') == read(STATE_DIR/'active.json') and
            proof.get('owner_instruction') == '继续完成任务', 'telemetry_resume_proof_unbound')
    require(transaction['candidate'] == a.id and transaction['previous'] == m['parent'] == previous.name and
            set(transaction['services']) == {'api', 'gateway', 'prometheus', 'alertmanager'} and
            transaction['backup_tier'] == FULL_TIER, 'telemetry_resume_transaction_invalid')
    require(proof.get('tools') == {n: sha(Path(__file__).with_name(n)) for n in
            ('production-stack.py', 'telemetry-monitoring.py')}, 'telemetry_resume_tool_binding_changed')
    transition = telemetry_transition()
    document = transition.frozen(telemetry_context(), candidate, m)
    require(document is not None and document['failure_policy'] == 'freeze', 'telemetry_resume_plan_required')
    protocol = telemetry_protocol()
    rows = inspect()
    failed = read(recovery)
    require(failed.get('phase') == 'frozen' and failed.get('maintenance_enabled') is True and
            failed.get('legacy_worker_started') is False and not failed.get('errors') and
            states(rows) == proof.get('states'), 'telemetry_resume_frozen_state_changed')
    for name in ('api', 'gateway'):
        row = rows[name]
        labels = row['Config']['Labels']
        require(not row['State']['Running'] and not row['State'].get('OOMKilled') and
                row['Image'] == m['images'][name] and row['RestartCount'] == 0 and
                labels.get('com.docker.compose.project') == PROJECT and
                labels.get('com.docker.compose.service') == name and
                labels.get('com.docker.compose.project.config_files') == str(candidate/'private/compose.json'),
                'telemetry_resume_writer_identity_changed')
    healthy({n: r for n, r in rows.items() if n not in ('api', 'gateway')})
    require(protocol.schema(telemetry_context(), rows)['s04'], 'telemetry_resume_schema_not_ready')
    require(protocol.gate(telemetry_context(), rows) == proof.get('freeze_gate') and
            proof['freeze_gate']['enabled'] is True, 'telemetry_resume_gate_ownership_changed')
    state = read(candidate/'telemetry-transition-state.json')
    require(state.get('phase') == 'ready_for_workers' and
            protocol.stop_owned_migrator(telemetry_context(), candidate, state)['stop_proven'],
            'telemetry_resume_migration_not_finished')
    archive = BACKUP_DIR/transaction['predeployment_backup']
    require(sha(archive) == transaction['backup_sha256'], 'telemetry_resume_backup_changed')
    if m.get('telemetry_repair'):
        telemetry_repair().backup_receipt(telemetry_repair_context(),candidate,m,archive)
    else:
        require(maintenance_receipt(archive, m, pm, transaction['backup_sha256']) is not None,
                'telemetry_resume_original_backup_unbound')
    source = ROOT/'runtime/source-builds/contexts'/document['source_sha256']
    command([sys.executable, str(Path(__file__).with_name('source-build.py')), 'verify', '--source', str(source)])
    preflight(read(candidate/'private/compose.json'), m['origin_environment'])
    drain()
    attempt = 'resume-'+a.attempt
    record_path = candidate/(attempt+'.json')
    require(not record_path.exists(), 'telemetry_resume_attempt_already_recorded')
    shutil.copyfile(recovery, candidate/('forward-recovery-before-'+attempt+'.json'))
    shutil.copyfile(pending, candidate/('pending-before-'+attempt+'.json'))
    record = {'passed': False, 'started_at': now(), 'candidate': a.id, 'attempt': attempt,
              'evidence_sha256': sha(evidence_path), 'pending_sha256': sha(pending),
              'manifest_sha256': digest(m), 'tools': proof['tools'], 'phase': 'admitting'}
    write(record_path, record)
    try:
        telemetry_monitoring().create_silence(telemetry_context(), candidate, document, attempt)
        record['released_gate'] = protocol.change_gate(telemetry_context(), rows, proof['freeze_gate'], False)
        record['phase'] = 'starting'; write(record_path, record)
        deploy(candidate, transaction['services'], startup_seconds=480 if m.get('telemetry_repair') else 60)
        accepted_gate = gate(candidate, transaction['services'], proof['states'])
        telemetry_monitoring().cleanup_silence(telemetry_context(), candidate, attempt=attempt)
        require(sha(pending) == record['pending_sha256'], 'telemetry_resume_pending_changed')
        set_active(candidate, accepted_gate)
        pending.unlink()
        shutil.copyfile(recovery,candidate/('recovery-before-accept-'+attempt+'.json'))
        write(recovery,{'phase':'resolved','candidate':a.id,'at':now(),
            'previous_recovery_sha256':sha(candidate/('recovery-before-accept-'+attempt+'.json'))})
        record.update(passed=True, phase='accepted', gate_file=accepted_gate, finished_at=now())
        write(record_path, record)
    except BaseException as error:
        record.update(phase='failed', error_type=type(error).__name__, finished_at=now())
        write(record_path, record)
        try:
            protocol.halt_forward_recovery(telemetry_context(), inspect(), transaction,
                {'s01': True, 's04': True}, 'telemetry_same_candidate_resume_failed')
        except protocol.TelemetryBlocked:
            pass
        finally:
            telemetry_monitoring().cleanup_silence(telemetry_context(), candidate, strict=False, attempt=attempt)
        raise Blocked('telemetry_same_candidate_resume_failed_frozen') from None


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
    plan.add_argument('--tier', choices=(FULL_TIER, FAST_TIER, STANDARD_TIER), default=FULL_TIER)
    plan.add_argument('--assessment', help='image-bound compatibility and targeted-test assessment for standard-fast')
    plan.add_argument('--allow-asset-shrink', action='store_true')
    plan.add_argument('--defer-backup-authorization')
    plan.add_argument('--telemetry-transition', help='frozen offline migration and owner-accepted freeze-on-failure evidence')
    check = sub.add_parser('preflight')
    check.add_argument('--id', required=True)
    activate = sub.add_parser('publish')
    activate.add_argument('--id', required=True)
    activate.add_argument('--backup')
    resume_telemetry = sub.add_parser('resume-telemetry')
    resume_telemetry.add_argument('--id', required=True)
    resume_telemetry.add_argument('--attempt', required=True, choices=('r1', 'r2', 'r3'))
    resume_telemetry.add_argument('--evidence', required=True)
    repair_prepare = sub.add_parser('prepare-telemetry-repair')
    repair_prepare.add_argument('--id', required=True)
    repair_prepare.add_argument('--evidence', required=True)
    repair_prepare.add_argument('--image', action='append', required=True)
    repair_prepare.add_argument('--telemetry-transition', required=True)
    repair_publish = sub.add_parser('publish-telemetry-repair')
    repair_publish.add_argument('--id', required=True)
    repair_publish.add_argument('--backup', required=True)
    maintenance = sub.add_parser('verify-backup')
    maintenance.add_argument('--backup', required=True)
    reconcile = sub.add_parser('reconcile-backup')
    reconcile.add_argument('--backup', required=True)
    resume = sub.add_parser('verify-resume')
    resume.add_argument('--receipt', required=True)
    rollback = sub.add_parser('rollback')
    rollback.add_argument('--backup-maintenance')
    sub.add_parser('status')
    migration = sub.add_parser('migration-stage')
    migration.add_argument('--id', required=True)
    migration.add_argument('--receipt', required=True)
    migration_start = sub.add_parser('migration-start')
    migration_start.add_argument('--id', required=True)
    migration_start.add_argument('--group', choices=('data', 'readonly', 'background'), required=True)
    migration_check = sub.add_parser('migration-checkpoint')
    migration_check.add_argument('--id', required=True)
    migration_check.add_argument('--group', choices=('data', 'readonly'), required=True)
    migration_check.add_argument('--evidence', required=True)
    migration_accept = sub.add_parser('migration-accept')
    migration_accept.add_argument('--id', required=True)
    watch = sub.add_parser('timing')
    watch.add_argument('--limit', type=int, default=10)
    a = parser.parse_args()
    if a.phase in ('publish-telemetry-repair','resume-telemetry'):
        global CANCEL_SAFE_COMMANDS
        CANCEL_SAFE_COMMANDS=True
        def interrupted(_signum,_frame):
            raise Blocked('telemetry_release_cancelled')
        signal.signal(signal.SIGTERM,interrupted)
        signal.signal(signal.SIGINT,interrupted)
    registered_business_host('release')
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
                actual_images = {s: row['Image'] for s, row in inspect().items()}
                expected = (read(release(pointer['release'])/'manifest.json')['images']
                            if pointer else {})
                print(json.dumps({'active': pointer, 'pending': pending.exists(),
                                  'actual_images': actual_images,
                                  'images_match_accepted': actual_images == expected,
                                  'transaction': ({k: v for k, v in read(pending).items()
                                      if k in ('candidate', 'previous', 'services', 'started_at')}
                                      if pending.exists() else None)}))
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
                    protocol = telemetry_protocol()
                    try:
                        transaction = {'kind': 'explicit_parent_rollback', 'candidate': current.name,
                                       'services': cm['services']}
                        if telemetry_transition().needs_schema_guard(telemetry_context(), transaction):
                            protocol.refuse_unsafe_parent(telemetry_context(), release(cm['parent']), transaction)
                    except protocol.TelemetryBlocked as error:
                        raise Blocked(str(error)) from None
                    write(pending, {'previous': cm['parent'], 'candidate': current.name,
                                    'services': cm['services'], 'before': states(inspect()),
                                    'started_at': now(), 'kind': 'explicit_parent_rollback'})
                restore_transaction(read(pending))
            elif a.phase == 'resume-telemetry':
                require(pending.exists(), 'telemetry_resume_pending_required')
                resume_frozen_telemetry(a)
            elif a.phase in ('prepare-telemetry-repair','publish-telemetry-repair'):
                require(pending.exists(), 'telemetry_repair_pending_required')
                repair=telemetry_repair()
                if a.phase=='prepare-telemetry-repair':repair.prepare(sys.modules[__name__],a)
                else:repair.publish(sys.modules[__name__],a)
            else:
                require(not pending.exists(), 'unfinished_release_requires_rollback')
                if a.phase.startswith('migration-'):
                    spec = importlib.util.spec_from_file_location(
                        'powersystem_migration_vm', Path(__file__).with_name('migration-vm.py'))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    module.dispatch(sys.modules[__name__], a)
                    record, value = persist_timing('ok')
                    print(json.dumps({'phase': a.phase, 'completed': True,
                                      'timing_file': str(record)}))
                    return
                {'prepare': prepare, 'bootstrap': bootstrap, 'preflight': verify_candidate,
                 'verify-backup': verify_backup_maintenance,
                 'reconcile-backup': verify_backup_maintenance,
                 'verify-resume': verify_failed_backup_resume,
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
