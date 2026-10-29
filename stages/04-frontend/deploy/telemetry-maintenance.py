#!/usr/bin/env python3
"""Telemetry schema/freeze boundaries shared by backup and release orchestration."""
import datetime
import hashlib
import re
import json
from pathlib import Path
import time


class TelemetryBlocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise TelemetryBlocked(reason)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def pg(ps, rows, query):
    # Passwords stay inside the existing PostgreSQL container environment.
    result = ps.command(['docker', 'exec', '-i', rows['postgres']['Id'], 'sh', '-c',
                         'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -c "$1"',
                         '--', "BEGIN;SET LOCAL statement_timeout='5s';"+query+';COMMIT;'], timeout=10)
    values = [json.loads(line) for line in result.decode().splitlines() if line.startswith('{')]
    require(len(values) == 1, 'telemetry_database_result_missing')
    return values[0]


def schema(ps, rows):
    return pg(ps, rows, "SELECT json_build_object('s01',exists(select 1 from information_schema.columns "
              "where table_schema='public' and table_name='telemetry_inbox' and column_name='business_state'),"
              "'s04',to_regclass('public.telemetry_snapshots') is not null)")


def gate(ps, rows):
    return pg(ps, rows, "SELECT json_build_object('enabled',enabled,'version',updated_at::text) "
              "FROM alarm_maintenance_state WHERE id=1")


def change_gate(ps, rows, expected, enabled):
    # Optimistic ownership: never undo a concurrent operator's later update.
    stamp = expected['version'].replace("'", "''")
    value = 'TRUE' if enabled else 'FALSE'
    return pg(ps, rows, "WITH changed AS (UPDATE alarm_maintenance_state SET enabled="+value+
              ",updated_at=clock_timestamp() WHERE id=1 AND updated_at='"+stamp+
              "'::timestamptz RETURNING enabled,updated_at) SELECT json_build_object('enabled',enabled,"
              "'version',updated_at::text) FROM changed")


def current_gateway(ps, expected):
    rows = ps.inspect()
    row = rows['gateway']
    require(row['Id'] == expected['id'] and row['Image'] == expected['image'] and
            row['RestartCount'] == expected['restarts'], 'telemetry_gateway_identity_changed')
    return rows, row


def pause(ps, state_file):
    path = Path(state_file)
    require(not path.exists(), 'telemetry_pause_state_exists')
    rows = ps.inspect()
    if (ps.STATE_DIR/'pending.json').exists():
        # Frozen repair backups preserve the already stopped writers and gate.
        # They never use a pending transaction as permission to start a worker.
        import importlib.util
        spec=importlib.util.spec_from_file_location('frozen_backup_repair',Path(__file__).with_name('telemetry-repair.py'))
        repair=importlib.util.module_from_spec(spec);spec.loader.exec_module(repair)
        proof_path, proof, rows=repair.backup_context(ps)
        ps.write(path, {'format':1,'kind':repair.FORMAT,'active':True,'phase':'preserved_frozen',
            'proof_path':str(proof_path),'proof_sha256':ps.sha(proof_path),'gateway_before':ps.states(rows)['gateway'],
            'gateway_after':ps.states(rows)['gateway'],'freeze_gate':proof['freeze_gate'],'started_at':now()})
        return
    installed = schema(ps, rows)
    if not installed['s04']:
        ps.write(path, {'format': 1, 'active': False, 'schema': installed})
        return
    ps.healthy(rows)
    maintenance = path.parent/'v4-maintenance.json'
    if not maintenance.exists():
        ps.write(maintenance, {'version': 1, 'active': ps.read(ps.STATE_DIR/'active.json'),
                               'before': ps.states(rows), 'started_at': now()})
    original = gate(ps, rows)
    expected = ps.states(rows)['gateway']
    state = {'format': 1, 'active': True, 'phase': 'admitting', 'started_at': now(),
             'prior_gate': original, 'gateway_before': expected}
    ps.write(path, state)
    admitted = change_gate(ps, rows, original, True)
    require(admitted['enabled'] is True, 'telemetry_freeze_not_enabled')
    state.update(owned_gate=admitted, phase='frozen')
    ps.write(path, state)
    current_gateway(ps, expected)
    ps.command(['docker', 'stop', '--time', '30', expected['id']])
    _, stopped = current_gateway(ps, expected)
    require(not stopped['State']['Running'] and stopped['State']['ExitCode'] == 0 and
            not stopped['State'].get('OOMKilled'), 'telemetry_gateway_not_cleanly_drained')
    state.update(phase='paused', paused_at=now(), stopped_exit_code=0)
    ps.write(path, state)


def resume(ps, state_file):
    path = Path(state_file)
    if not path.exists():
        return
    state = ps.read(path)
    if state.get('kind')=='telemetry-frozen-repair/v1':
        import importlib.util
        spec=importlib.util.spec_from_file_location('frozen_resume_repair',Path(__file__).with_name('telemetry-repair.py'))
        repair=importlib.util.module_from_spec(spec);spec.loader.exec_module(repair)
        require(ps.sha(state['proof_path'])==state['proof_sha256'],'telemetry_frozen_backup_proof_changed')
        repair.admit(ps,state['proof_path'],states_after=ps.states(ps.inspect()))
        state.update(phase='preserved_frozen',resumed_at=now());ps.write(path,state)
        return
    if not state.get('active') or state.get('phase') == 'resumed':
        return
    require(state.get('phase') in ('frozen', 'paused') and 'owned_gate' in state,
            'telemetry_pause_requires_manual_recovery')
    rows, row = current_gateway(ps, state['gateway_before'])
    require(gate(ps, rows) == state['owned_gate'], 'telemetry_freeze_ownership_changed')
    # An interrupted pause may already have stopped the writer; a clean exit is
    # required before a restart can ever be described as a drained backup.
    if not row['State']['Running']:
        require(row['State']['ExitCode'] == 0 and not row['State'].get('OOMKilled'),
                'telemetry_gateway_not_cleanly_drained')
        ps.command(['docker', 'start', state['gateway_before']['id']])
    deadline = time.monotonic()+60
    while True:
        rows, row = current_gateway(ps, state['gateway_before'])
        require(row['State']['Running'], 'telemetry_gateway_resume_failed')
        health = row['State'].get('Health', {}).get('Status', 'none')
        if health in ('none', 'healthy'):
            break
        require(health == 'starting' and time.monotonic() < deadline,
                'telemetry_gateway_resume_unhealthy')
        time.sleep(1)
    restored = change_gate(ps, rows, state['owned_gate'], state['prior_gate']['enabled'])
    require(restored['enabled'] == state['prior_gate']['enabled'], 'telemetry_gate_restore_failed')
    state.update(phase='resumed', resumed_at=now(), restored_gate=restored,
                 gateway_after=ps.states(rows)['gateway'])
    ps.write(path, state)


def receipt(ps, state_file):
    path = Path(state_file)
    if not path.exists():
        return None
    value = ps.read(path)
    if value.get('kind')=='telemetry-frozen-repair/v1':
        require(value.get('phase')=='preserved_frozen' and value.get('resumed_at') and
                value.get('gateway_before')==value.get('gateway_after') and
                value.get('freeze_gate',{}).get('enabled') is True,'telemetry_frozen_backup_unproven')
        return value
    if not value.get('active'):
        return None
    require(value.get('phase') == 'resumed' and value.get('stopped_exit_code') == 0,
            'telemetry_backup_resume_unproven')
    return value


def validate_receipt(before, after, value):
    if value is None:
        return False
    require(value.get('format') == 1 and value.get('active') is True and
            value.get('phase') == 'resumed' and value.get('stopped_exit_code') == 0,
            'telemetry_backup_receipt_invalid')
    require(value.get('gateway_before') == before.get('gateway') and
            value.get('gateway_after') == after.get('gateway'), 'telemetry_backup_gateway_unbound')
    require(value.get('owned_gate', {}).get('enabled') is True and
            value.get('restored_gate', {}).get('enabled') == value.get('prior_gate', {}).get('enabled') and
            isinstance(value.get('prior_gate', {}).get('enabled'), bool),
            'telemetry_backup_gate_restore_unproven')
    first = datetime.datetime.fromisoformat(value['started_at'])
    paused = datetime.datetime.fromisoformat(value['paused_at'])
    resumed = datetime.datetime.fromisoformat(value['resumed_at'])
    started = datetime.datetime.fromisoformat(after['gateway']['started_at'].replace('Z', '+00:00'))
    require(first.tzinfo is not None and first <= paused <= started <= resumed,
            'telemetry_backup_gateway_outside_window')
    return True


def stop_owned_migrator(ps, candidate, state):
    if state.get('kind')=='inherited_completed_schema':
        try:
            require(candidate.name=='tr-s04-20261028-r4' and state.get('from_candidate')=='tr-s04-20261028-r3',
                    'inherited_migration_scope_invalid')
            original=ps.STATE_DIR/'releases/tr-s04-20261028-r3'
            path=original/'telemetry-transition-state.json'
            require(ps.sha(path)==state.get('original_state_sha256') and
                    ps.read(path).get('phase')=='ready_for_workers','inherited_migration_state_changed')
            prior=stop_owned_migrator(ps,original,ps.read(path))
            unexpected=ps.command(['docker','ps','-aq','--filter','name=^/telemetry-migrate-'+candidate.name+'$'],timeout=10)
            return {'stop_proven':prior['stop_proven'] and not unexpected.strip(),
                    'reason':'inherited_completed_schema_checked'}
        except Exception:
            return {'stop_proven':False,'reason':'inherited_schema_stop_unproven'}
    migration = state.get('migrator')
    if migration is None:
        return {'stop_proven': state.get('phase') in ('admitting', 'frozen', 'writers_stopped'),
                'reason': 'not_started'}
    expected = 'telemetry-migrate-'+candidate.name
    if migration.get('name') != expected:
        return {'stop_proven': False, 'reason': 'migration_name_unbound'}
    try:
        ids = ps.command(['docker', 'ps', '-aq', '--filter', 'name=^/'+expected+'$'], timeout=10).decode().split()
        require(len(ids) <= 1, 'migration_identity_ambiguous')
        if not ids:
            return {'stop_proven': True, 'reason': 'owned_container_absent'}
        row = json.loads(ps.command(['docker', 'inspect', ids[0]], timeout=10))[0]
        labels = row.get('Config', {}).get('Labels', {})
        require(row['Image'] == migration['image'] and
                labels.get('com.docker.compose.project') == getattr(ps, 'PROJECT', 'powersystem-stage4') and
                labels.get('com.docker.compose.service') == 'api' and
                str(labels.get('com.docker.compose.oneoff', '')).lower() == 'true',
                'migration_container_not_owned')
        ps.command(['docker', 'stop', '--time', '30', ids[0]], timeout=45)
        require(not ps.command(['docker', 'ps', '-q', '--filter', 'id='+ids[0]], timeout=10).strip(),
                'migration_still_running')
        return {'stop_proven': True, 'reason': 'owned_container_stopped', 'id': ids[0]}
    except Exception:
        return {'stop_proven': False, 'reason': 'migration_stop_unproven'}


def compatible_parent(ps, previous, installed):
    if not installed.get('s04'):
        return False
    expected = None
    for _ in range(10):
        manifest = ps.read(previous/'manifest.json')
        pair = {k: manifest['images'].get(k) for k in ('api', 'gateway')}
        if expected is None:
            expected = pair
        if pair != expected:
            return False
        marker = manifest.get('telemetry_transition')
        if marker:
            path = previous/'telemetry-transition.json'
            if (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != marker.get('sha256') or
                    marker.get('sha256') != manifest.get('files', {}).get(path.name)):
                return False
            proof = ps.read(path)
            builds = {b['service']: b for b in manifest.get('source_builds', [])}
            return (proof.get('kind') == 'telemetry-forward-only/v1' and
                    proof.get('large_table_evidence', {}).get('passed') is True and
                    proof.get('protocol_evidence', {}).get('passed') is True and
                    all(builds.get(k, {}).get('image') == pair[k] and
                        builds.get(k, {}).get('source_sha256') == proof.get('source_sha256')
                        for k in ('api', 'gateway')))
        parent = manifest.get('parent')
        if not isinstance(parent, str) or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', parent) is None:
            return False
        previous = ps.STATE_DIR/'releases'/parent
    return False


def halt_forward_recovery(ps, rows, transaction, installed, reason):
    if transaction.get('kind') == 'explicit_parent_rollback':
        raise TelemetryBlocked('telemetry_legacy_parent_incompatible')
    record = {'format': 1, 'passed': False, 'schema': installed, 'transaction': transaction,
              'stopped': [], 'errors': {}, 'at': now(), 'maintenance_enabled': False,
              'legacy_worker_started': False, 'reason': reason, 'phase': 'freezing'}
    path = ps.STATE_DIR/'telemetry-forward-recovery-required.json'
    def persist():
        try: ps.write(path, record)
        except Exception: record['errors']['evidence'] = 'write_failed'
    persist()
    try:
        state = gate(ps, rows)
        record['maintenance_enabled'] = change_gate(ps, rows, state, True)['enabled'] is True
    except Exception:
        record['errors']['gate'] = 'freeze_unproven'
    for service in ('api', 'gateway'):
        try:
            row = ps.inspect()[service]
            require(row['Id'] == rows[service]['Id'] and row['Image'] == rows[service]['Image'],
                    'telemetry_recovery_identity_changed')
            ps.command(['docker', 'stop', '--time', '30', row['Id']], timeout=45)
            require(not ps.inspect()[service]['State']['Running'], 'telemetry_recovery_writer_still_running')
            record['stopped'].append(service)
        except Exception:
            record['errors'][service] = 'stop_unproven'
        persist()
    candidate_name = transaction.get('candidate')
    if isinstance(candidate_name, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', candidate_name):
        candidate = ps.STATE_DIR/'releases'/candidate_name
        state_file = candidate/'telemetry-transition-state.json'
        if state_file.is_file():
            try:
                record['migrator'] = stop_owned_migrator(ps, candidate, ps.read(state_file))
            except Exception:
                record['errors']['migrator'] = 'state_unreadable'
    record['phase'] = 'frozen' if len(record['stopped']) == 2 and record['maintenance_enabled'] else 'recovery_incomplete'
    persist()
    raise TelemetryBlocked(reason)


def refuse_unsafe_parent(ps, previous, transaction):
    rows = ps.inspect()
    try:
        installed = schema(ps, rows)
    except Exception:
        halt_forward_recovery(ps, rows, transaction, {'state': 'unknown'},
                              'telemetry_schema_probe_failed_rollback_refused')
    if not installed['s01']:
        return
    if not compatible_parent(ps, previous, installed):
        halt_forward_recovery(ps, rows, transaction, installed,
                              'telemetry_schema_requires_forward_recovery')


def main():
    import argparse
    import importlib.util
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('pause', 'resume'))
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    controller = Path(__file__).with_name('production-stack.py')
    spec = importlib.util.spec_from_file_location('telemetry_production_stack', controller)
    ps = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ps
    spec.loader.exec_module(ps)
    # The caller owns backup.sh's deployment/backup locks and staging directory.
    state = Path(args.state).resolve()
    require(state.is_relative_to(ps.BACKUP_DIR) and state.parent.exists(), 'telemetry_backup_state_scope')
    {'pause': pause, 'resume': resume}[args.operation](ps, state)


if __name__ == '__main__':
    main()
