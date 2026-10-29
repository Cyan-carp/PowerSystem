#!/usr/bin/env python3
"""Explicit, full-backup-only offline schema boundary for telemetry releases."""
from pathlib import Path
import json
import time

TRANSITION_CANDIDATES = frozenset(('tr-s04-20261028-r2', 'tr-s04-20261028-r3', 'tr-s04-20261028-r4'))


def validate(ps, document, parent, services, tier, builds, candidate=None):
    ps.require(document.get('format') == 1 and document.get('kind') == 'telemetry-forward-only/v1',
               'telemetry_transition_format_invalid')
    ps.require(document.get('candidate') in TRANSITION_CANDIDATES and
               (candidate is None or document['candidate'] == candidate),
               'telemetry_transition_candidate_unbound')
    ps.require(document.get('parent') == parent and
               set(services) == {'api', 'gateway', 'prometheus', 'alertmanager'} and tier == 'full',
               'telemetry_transition_scope_invalid')
    ps.require(document.get('failure_policy') == 'freeze' and
               document.get('owner_accepted_freeze_on_failure') is True and
               document.get('owner_instruction') == '失败时冻结遥测写入、保留现场，修复后恢复',
               'telemetry_freeze_authorization_required')
    ps.require(document.get('migration_timeout_seconds') == 900, 'telemetry_migration_budget_unverified')
    hashes = {v['source_sha256'] for v in builds if v['service'] in ('api', 'gateway')}
    ps.require(len(hashes) == 1 and document.get('source_sha256') in hashes and
               {v['service'] for v in builds} == {'api', 'gateway'}, 'telemetry_source_binding_invalid')
    source = ps.ROOT/'runtime/source-builds/contexts'/document['source_sha256']
    files = ps.read(source/'source-manifest.json')['files']
    ps.require(document.get('schema_entry_sha256') == files['stages/02-backend/cmd/api/main.go']['sha256'],
               'telemetry_migration_entry_unbound')
    large = document.get('large_table_evidence', {})
    ps.require(large.get('passed') is True and large.get('target_rows', 0) >= 14653886 and
               large.get('candidate_changed') == [] and large.get('offline_15m', {}).get('exit') == 0 and
               large.get('repeat_default_30s', {}).get('exit') == 0 and
               0 < large.get('offline_15m', {}).get('seconds', 0) < 900,
               'telemetry_large_migration_evidence_required')
    ps.require(isinstance(large.get('before'), dict) and
               large['before'].get('rows') == large.get('target_rows') and
               large.get('before') == large.get('after') and
               large.get('legacy_null_count') == large.get('target_rows'),
               'telemetry_migration_preservation_unproven')
    protocol = document.get('protocol_evidence', {})
    ps.require(protocol.get('passed') is True and protocol.get('source_changed') == [] and
               protocol.get('real_gateway_linux_sigterm') is True and
               protocol.get('maintenance_receipt_valid') is True and
               protocol.get('legacy_worker_started') is False and protocol.get('pending_retained') is True,
               'telemetry_protocol_evidence_required')
    ps.require(document.get('protocol_helper_sha256') == files['stages/04-frontend/deploy/telemetry-maintenance.py']['sha256'],
               'telemetry_protocol_source_unbound')
    return document


def require_if_needed(ps, builds, document):
    api = next((value for value in builds if value['service'] == 'api'), None)
    if api is None:
        return
    source = ps.ROOT/'runtime/source-builds/contexts'/api['source_sha256']
    files = ps.read(source/'source-manifest.json')['files']
    if 'stages/02-backend/deploy/postgres/016_telemetry_realtime.sql' not in files:
        return
    installed = ps.telemetry_protocol().schema(ps.telemetry_context(), ps.inspect())
    ps.require(installed['s04'] or document is not None, 'telemetry_offline_transition_required')


def frozen(ps, candidate, manifest):
    marker = manifest.get('telemetry_transition')
    if marker is None:
        return None
    path = candidate/'telemetry-transition.json'
    ps.require(marker.get('sha256') == manifest['files'].get(path.name) == ps.sha(path),
               'telemetry_transition_frozen_binding_changed')
    return validate(ps, ps.read(path), manifest['parent'], manifest['services'],
                    manifest['backup_tier'], manifest['source_builds'], candidate.name)


def before_cutover(ps):
    protocol = ps.telemetry_protocol()
    rows = ps.inspect()
    # Existing long backlog needs its own authorization. Messages accumulated
    # after this controlled stop are transport recovery within this release.
    value = protocol.pg(ps.telemetry_context(), rows,
        "SELECT json_build_object('oldest_age_seconds',COALESCE(EXTRACT(EPOCH FROM "
        "clock_timestamp()-min(received_at)),0)) FROM telemetry_inbox WHERE processed_at IS NULL")
    ps.require(value['oldest_age_seconds'] <= 15, 'telemetry_existing_backlog_requires_authorization')
    ps.require(protocol.gate(ps.telemetry_context(), rows)['enabled'] is False,
               'telemetry_existing_maintenance_requires_review')


def apply(ps, candidate, manifest, transaction):
    document = frozen(ps, candidate, manifest)
    if document is None:
        return
    protocol = ps.telemetry_protocol()
    context = ps.telemetry_context()
    before_cutover(ps)
    rows = ps.inspect()
    state = {'format': 1, 'phase': 'admitting', 'started_at': ps.now(),
             'prior_gate': protocol.gate(context, rows), 'before': ps.states(rows)}
    path = candidate/'telemetry-transition-state.json'
    ps.write(path, state)
    state['owned_gate'] = protocol.change_gate(context, rows, state['prior_gate'], True)
    state['phase'] = 'frozen'
    ps.write(path, state)
    ps.telemetry_monitoring().create_silence(ps, candidate, document)
    # No old API or gateway can remain alive while any forward-only DDL commits.
    for name in ('api', 'gateway'):
        actual = ps.inspect()[name]
        ps.require(actual['Id'] == rows[name]['Id'] and actual['Image'] == rows[name]['Image'],
                   'telemetry_cutover_identity_changed')
        ps.command(['docker', 'stop', '--time', '30', actual['Id']])
        stopped = ps.inspect()[name]
        ps.require(not stopped['State']['Running'] and stopped['State']['ExitCode'] != 137 and
                   not stopped['State'].get('OOMKilled'), 'telemetry_old_writer_stop_failed')
    state['phase'] = 'writers_stopped'
    ps.write(path, state)
    log = candidate/'telemetry-offline-migration.log'
    start = time.monotonic()
    name = 'telemetry-migrate-'+candidate.name
    state['migrator'] = {'name': name, 'image': manifest['images']['api']}
    ps.write(path, state)
    try:
        ps.command(ps.compose(candidate)+['run', '--rm', '--no-deps', '--pull', 'never', '-T', '--entrypoint', '/app/server',
                   '--name', name, 'api', '-migrate-only', '-migration-timeout=900s'], log, timeout=930)
    except Exception:
        state['migrator_stop'] = protocol.stop_owned_migrator(context, candidate, state)
        state['phase'] = 'migration_failed_stopped' if state['migrator_stop']['stop_proven'] else 'migration_stop_unproven'
        ps.write(path, state)
        raise
    ps.require(protocol.schema(context, ps.inspect())['s04'], 'telemetry_schema_not_ready')
    state.update(phase='schema_ready', migration_seconds=time.monotonic()-start)
    ps.write(path, state)
    state['restored_gate'] = protocol.change_gate(context, ps.inspect(), state['owned_gate'], state['prior_gate']['enabled'])
    state.update(phase='ready_for_workers', finished_at=ps.now())
    ps.write(path, state)


def ensure_migrator_stopped(ps, candidate):
    path = candidate/'telemetry-transition-state.json'
    if not path.exists():
        return
    state = ps.read(path)
    protocol = ps.telemetry_protocol()
    state['migrator_stop'] = protocol.stop_owned_migrator(ps.telemetry_context(), candidate, state)
    ps.write(path, state)
    ps.require(state['migrator_stop']['stop_proven'], 'telemetry_migrator_requires_manual_recovery')


def restore_pre_schema_gate(ps, candidate):
    path = candidate/'telemetry-transition-state.json'
    if not path.exists():
        return
    state = ps.read(path)
    ps.require(not state.get('migrator') or state.get('migrator_stop', {}).get('stop_proven') is True,
               'telemetry_migrator_requires_manual_recovery')
    protocol = ps.telemetry_protocol()
    rows = ps.inspect()
    actual = protocol.gate(ps.telemetry_context(), rows)
    if state.get('restored_gate') == actual and actual['enabled'] == state['prior_gate']['enabled']:
        return
    ps.require('owned_gate' in state, 'telemetry_cutover_ownership_unproven')
    protocol.change_gate(ps.telemetry_context(), rows, state['owned_gate'], state['prior_gate']['enabled'])


def needs_schema_guard(ps, transaction):
    if not set(transaction.get('services', [])) & {'api', 'gateway'}:
        return False
    name = transaction.get('candidate')
    if not name:
        return False
    manifest = ps.read(ps.release(name)/'manifest.json')
    if manifest.get('telemetry_transition'):
        return True
    for build in manifest.get('source_builds', []):
        if build.get('service') not in ('api', 'gateway'):
            continue
        source = ps.ROOT/'runtime/source-builds/contexts'/build['source_sha256']
        files = ps.read(source/'source-manifest.json')['files']
        if 'stages/02-backend/deploy/postgres/016_telemetry_realtime.sql' in files:
            return True
    return False
