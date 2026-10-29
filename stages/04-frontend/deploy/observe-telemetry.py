"""Owner-started, read-only telemetry observation on the registered business VM.

Writes projected samples.jsonl and summary.json, never tokens, payloads or raw
logs. Does not accept a release, alter a queue, restart services or repair data.
"""
import argparse
import base64
import collections
import datetime as dt
import importlib.util
import json
import math
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PREFIX = 'powersystem_telemetry_'
METRICS = ('fresh_devices', 'expected_devices', 'probe_age_seconds',
           'cache_sample_worst_age_seconds', 'queue_oldest_age_seconds',
           'success_total', 'retry_total', 'quarantine_total',
           'scheduler_visit_total', 'scheduler_wrap_total',
           'ingress_ack_requested_total', 'inbox_receive_age_seconds',
           'probe_pg_ok', 'probe_redis_ok', 'probe_td_ok', 'maintenance')
HEADS = """WITH RECURSIVE h AS (
(SELECT device_id,id,business_state,failure_code,attempts,received_at FROM telemetry_inbox
 WHERE processed_at IS NULL AND device_id>0 ORDER BY device_id,id LIMIT 1)
UNION ALL SELECT n.* FROM h CROSS JOIN LATERAL (
 SELECT device_id,id,business_state,failure_code,attempts,received_at FROM telemetry_inbox
 WHERE processed_at IS NULL AND device_id>h.device_id ORDER BY device_id,id LIMIT 1)n)
SELECT row_to_json(s) FROM (SELECT * FROM h LIMIT 500)s"""
ENERGY = """SELECT json_build_object(
'dirty_count',(SELECT count(*) FROM energy_dirty_days),
'oldest_dirty',(SELECT min(changed_at) FROM energy_dirty_days),
'days',(SELECT json_agg(s) FROM (SELECT source_quality,count(*) AS count,
 min(updated_at) AS oldest_update,max(updated_at) AS newest_update,
 min(covered_ms) AS min_covered_ms,max(gap_count) AS max_gap_count
 FROM energy_daily GROUP BY source_quality)s))"""
ERRORS = ('energy_rebuild_failed', 'out-of-order_commit',
          'puback_packetId_not_found', 'emqx_persistent_session_ds_replay_inconsistency',
          'gateway_admission_lookup_retry', 'gateway_admission_persist_retry',
          'gateway_admission_commit_retry', 'tdengine_batch_deferred')


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def decode_rows(value):
    data = '\n'.join(x for x in value.splitlines() if x not in ('BEGIN', 'SET', 'COMMIT'))
    rows = []
    while data.strip():
        item, n = json.JSONDecoder().raw_decode(data.lstrip())
        rows.append(item)
        data = data.lstrip()[n:]
    return rows


def delta_heads(first, last):
    old = {r['device_id']: r['id'] for r in first}
    new = {r['device_id']: r['id'] for r in last}
    return {'initial_devices': len(old),
            'advanced_or_drained': sorted(k for k, v in old.items() if k not in new or new[k] > v),
            'unchanged': sorted(k for k, v in old.items() if new.get(k) == v),
            'regressed': sorted(k for k, v in old.items() if k in new and new[k] < v)}


class Probe:
    def __init__(self, root):
        if socket.gethostname() != 'powersystem-prod':
            raise RuntimeError('wrong_business_host')
        spec = importlib.util.spec_from_file_location('observation_stack', root / 'stages/04-frontend/deploy/production-stack.py')
        self.ps = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.ps
        spec.loader.exec_module(self.ps)
        self.stack = root / 'runtime/production-stack'
        self.initial = None

    def pg(self, sql):
        query = "BEGIN READ ONLY;SET LOCAL statement_timeout='3s';" + sql + ';COMMIT;'
        p = subprocess.run(['docker', 'exec', self.rows['postgres']['Id'], 'sh', '-c',
                            'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -c "$1"',
                            '--', query], capture_output=True, text=True, timeout=6)
        if p.returncode:
            raise RuntimeError('pg_query_failed')
        return decode_rows(p.stdout)

    def guarded(self, name, fn, out):
        try:
            out[name] = fn()
        except Exception as exc:
            out.setdefault('errors', {})[name] = type(exc).__name__

    def http(self, url):
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.read(2_000_000).decode()

    def metrics(self, base):
        out = {}
        for line in self.http(base + '/metrics').splitlines():
            if not line.startswith(PREFIX):
                continue
            name, value = line.rsplit(' ', 1)
            name = name.removeprefix(PREFIX)
            if name in METRICS:
                number = float(value)
                out[name] = number if math.isfinite(number) else None
        return out

    def td_samples(self, turn):
        devices = self.pg('SELECT json_agg(id ORDER BY id) FROM devices WHERE deleted_at IS NULL')[0]
        ids = [devices[(turn * 5 + i) % len(devices)] for i in range(min(5, len(devices)))]
        env = dict(x.split('=', 1) for x in self.rows['api']['Config']['Env'])
        database = env.get('TDENGINE_DATABASE', 'powersystem_stage2')
        if not re.fullmatch('[a-zA-Z_][a-zA-Z_0-9]*', database):
            raise RuntimeError('invalid_td_database')
        ip = next(v['IPAddress'] for v in self.rows['tdengine']['NetworkSettings']['Networks'].values() if v.get('IPAddress'))
        auth = base64.b64encode((env.get('TDENGINE_USER', 'root') + ':' + env['TDENGINE_ROOT_PASSWORD']).encode()).decode()
        result = []
        for device_id in ids:
            req = urllib.request.Request('http://' + ip + ':6041/rest/sql',
                data=('SELECT ts,seq FROM ' + database + '.t_device_' + str(int(device_id)) + ' ORDER BY ts DESC LIMIT 1').encode(),
                headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'text/plain'})
            with urllib.request.urlopen(req, timeout=2) as response:
                body = json.load(response)
            if body.get('code') != 0 or len(body.get('data', [])) != 1:
                result.append({'device_id': device_id, 'error': 'td_latest_unavailable'})
                continue
            stamp, seq = body['data'][0]
            instant = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            age = (dt.datetime.now(dt.timezone.utc) - instant).total_seconds()
            result.append({'device_id': device_id, 'ts': stamp, 'seq': seq, 'age_seconds': round(age, 3)})
        return result

    def sample(self, turn, since):
        out = {'utc': utc(), 'turn': turn, 'errors': {}}
        self.rows = self.ps.inspect()
        states = self.ps.states(self.rows)
        if self.initial is None:
            self.initial = states
        out['states'] = states
        out['states_changed'] = states != self.initial
        out['oom_services'] = [k for k, v in self.rows.items() if v['State'].get('OOMKilled')]
        out['active_sha256'] = self.ps.sha(self.stack / 'active.json')
        out['pending_sha256'] = self.ps.sha(self.stack / 'pending.json') if (self.stack / 'pending.json').exists() else None
        ip = next(v['IPAddress'] for v in self.rows['api']['NetworkSettings']['Networks'].values() if v.get('IPAddress'))
        base = 'http://' + ip + ':8080'
        self.guarded('metrics', lambda: self.metrics(base), out)
        try:
            self.http(base + '/ready')
            out['ready_http'] = 200
        except urllib.error.HTTPError as exc:
            out['ready_http'] = exc.code
        except Exception as exc:
            out['errors']['ready'] = type(exc).__name__
        self.guarded('heads', lambda: self.pg(HEADS), out)
        self.guarded('energy', lambda: self.pg(ENERGY)[0], out)
        self.guarded('pending_count', lambda: self.pg("SELECT count(*)::text::json FROM telemetry_inbox WHERE processed_at IS NULL")[0], out)
        self.guarded('td_latest', lambda: self.td_samples(turn), out)
        events = {}
        for service in ('api', 'gateway', 'emqx'):
            def read_events():
                p = subprocess.run(['docker', 'logs', '--since', since, '--tail', '300', self.rows[service]['Id']],
                                   capture_output=True, text=True, timeout=5)
                if p.returncode:
                    raise RuntimeError('container_logs_unavailable')
                lines = (p.stdout + '\n' + p.stderr).splitlines()
                if service == 'emqx':
                    lines = [s for s in lines if 'powersystem-stage2' in s]
                return {'counts': {key: sum(key in s for s in lines) for key in ERRORS},
                        'tail_limit': 300, 'possibly_truncated': len(lines) >= 300}
            self.guarded(service, read_events, events)
        out['events'] = events
        return out


def summarize(samples, duration, elapsed, interrupted=False):
    reasons = set()
    for row in samples:
        m = row.get('metrics', {})
        if row.get('errors'):
            reasons.add('probe_error')
        if row.get('ready_http') != 200:
            reasons.add('api_not_ready')
        if m.get('fresh_devices') != 143 or m.get('expected_devices') != 143:
            reasons.add('fleet_not_fresh_143')
        age, probe = m.get('cache_sample_worst_age_seconds'), m.get('probe_age_seconds')
        if age is None or probe is None or probe > 10 or age + probe > 15:
            reasons.add('sample_freshness_not_proven')
        if any(m.get(key) != 1 for key in ('probe_pg_ok', 'probe_redis_ok', 'probe_td_ok')) or m.get('maintenance') != 0:
            reasons.add('dependency_or_maintenance')
        if row.get('states_changed') or row.get('oom_services'):
            reasons.add('container_changed_or_oom')
        if any(r.get('error') or r.get('age_seconds', -1) < -60 or r.get('age_seconds', 999) > 15 for r in row.get('td_latest', [])):
            reasons.add('td_current_sample_not_proven')
        for event in row.get('events', {}).values():
            if isinstance(event, dict) and any(event.get('counts', {}).values()):
                reasons.add('runtime_error_event')
        if row.get('events', {}).get('errors'):
            reasons.add('log_probe_error')
        if len(row.get('td_latest', [])) < 1:
            reasons.add('td_current_sample_not_proven')
    if len(samples) < 2 or elapsed < duration or interrupted:
        reasons.add('observation_incomplete')
    first, last = (samples[0], samples[-1]) if samples else ({}, {})
    counts = [r['pending_count'] for r in samples if isinstance(r.get('pending_count'), int)]
    if counts and counts[-1] > counts[0]:
        reasons.add('pending_increased')
    if first.get('active_sha256') != last.get('active_sha256') or first.get('pending_sha256') != last.get('pending_sha256'):
        reasons.add('release_pointer_changed')
    progress = delta_heads(first.get('heads', []), last.get('heads', []))
    if progress['unchanged'] or progress['regressed']:
        reasons.add('history_head_not_advanced')
    if counts and counts[0] > 0 and last.get('metrics', {}).get('success_total', 0) <= first.get('metrics', {}).get('success_total', 0):
        reasons.add('business_success_not_advanced')
    td_seen = collections.defaultdict(list)
    for row in samples:
        for item in row.get('td_latest', []):
            if 'ts' in item:
                td_seen[item['device_id']].append(item['ts'])
    td_advanced = sorted(k for k, v in td_seen.items() if len(v) >= 2 and v[-1] > v[0])
    energy_first = first.get('energy', {})
    energy_last = last.get('energy', {})
    latest_energy = lambda e: max((v.get('newest_update') or '' for v in e.get('days') or [] if v.get('source_quality') != 'legacy_unknown'), default='')
    if energy_last.get('dirty_count', 0) > 0 and latest_energy(energy_last) <= latest_energy(energy_first):
        reasons.add('dirty_energy_not_advanced')
    # Rotation samples 5 devices/round; current TD age is checked separately.
    return {'result': 'anomaly' if reasons else 'short_observation_pass',
            'reasons': sorted(reasons), 'rounds': len(samples),
            'requested_seconds': duration, 'actual_seconds': round(elapsed, 3),
            'started_at': first.get('utc'), 'ended_at': last.get('utc'),
            'pending_first': counts[0] if counts else None, 'pending_last': counts[-1] if counts else None,
            'history': progress, 'td_devices_sampled': len(td_seen), 'td_devices_advanced': td_advanced,
            'energy_first': energy_first, 'energy_last': energy_last,
            'acceptance_written': False, 'long_term_stability_claimed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/powersystem'))
    parser.add_argument('--duration', type=int, default=300)
    parser.add_argument('--interval', type=int, default=10)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    if not 10 <= args.duration <= 1800 or not 5 <= args.interval <= 60:
        parser.error('duration 10..1800, interval 5..60 seconds')
    args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    probe = Probe(args.root)
    start = time.monotonic()
    samples = []
    interrupted = False
    since = utc()
    try:
        with (args.output_dir / 'samples.jsonl').open('x', encoding='utf-8') as handle:
            while True:
                try:
                    row = probe.sample(len(samples), since)
                except Exception as exc:
                    row = {'utc': utc(), 'errors': {'sample': type(exc).__name__}}
                since = row['utc']
                samples.append(row)
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                handle.flush()
                print(row['utc'], 'round=' + str(len(samples)), 'ready=' + str(row.get('ready_http')), flush=True)
                if time.monotonic() - start >= args.duration:
                    break
                time.sleep(min(args.interval, max(0, args.duration - (time.monotonic() - start))))
    except KeyboardInterrupt:
        interrupted = True
    summary = summarize(samples, args.duration, time.monotonic() - start, interrupted)
    (args.output_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['result'] == 'short_observation_pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
