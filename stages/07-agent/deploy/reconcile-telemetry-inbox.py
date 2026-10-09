#!/usr/bin/env python3
"""Replay the three simulated devices' durable inbox into TDengine after a schema migration.

The API processes an inbox row only after it is present in TDengine. A gateway
connection invalidated by ALTER TABLE can leave a durable inbox gap. Replaying
the exact stored payload is idempotent by the TDengine timestamp primary key.
"""

import argparse
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from decimal import Decimal


def run(args):
    return subprocess.run(args, text=True, capture_output=True, check=True).stdout


def number(value):
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError('non-finite telemetry value')
    return format(parsed, 'f')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', help='write the stored samples to TDengine')
    parser.add_argument('--max-id', type=int, help='inclusive inbox snapshot boundary from dry run')
    parser.add_argument('--max-rows', type=int, default=10000)
    parser.add_argument('--database', default='powersystem_stage2')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', args.database):
        raise ValueError('invalid database identifier')
    if args.max_rows < 1 or (args.max_id is not None and args.max_id < 1):
        raise ValueError('invalid row limit')
    boundary = f' AND i.id <= {args.max_id}' if args.max_id is not None else ''
    query = ("SELECT row_to_json(t) FROM (SELECT i.id,i.device_id,i.run_id,i.seq,i.ts_ms,"
             "i.payload,d.device_code,d.station_code FROM telemetry_inbox i "
             "JOIN devices d ON d.id=i.device_id WHERE i.processed_at IS NULL "
             "AND d.device_code IN ('INV-1001','INV-1002','INV-1003')" + boundary +
             " ORDER BY i.id) t")
    output = run(['docker', 'exec', 'powersystem-stage4-postgres-1', 'psql', '-U',
                  'powersystem', '-d', 'powersystem', '-Atqc', query])
    rows = [json.loads(line, parse_float=Decimal) for line in output.splitlines() if line]
    if len(rows) > args.max_rows:
        raise ValueError(f'inbox row count exceeds --max-rows: {len(rows)}')
    groups = defaultdict(list)
    digest = hashlib.sha256()
    for row in rows:
        sample = row['payload']
        if (sample['device_id'] != row['device_code'] or
                sample['station_id'] != row['station_code'] or
                sample['run_id'] != row['run_id'] or
                int(sample['seq']) != row['seq'] or int(sample['ts_ms']) != row['ts_ms'] or
                row['device_code'] not in ('INV-1001', 'INV-1002', 'INV-1003')):
            raise ValueError(f'inbox payload mismatch at id {row["id"]}')
        device_id = int(row['device_id'])
        values = [str(int(row['ts_ms'])), str(int(row['seq']))] + [number(sample[key])
                  for key in ('voltage', 'current', 'temperature', 'power')] + [
                      str(int(sample['status'])), str(int(sample['fault_code']))]
        groups[device_id].append('(' + ','.join(values) + ')')
        digest.update(f'{row["id"]}:{device_id}:{row["ts_ms"]}:{row["seq"]}\n'.encode())
    print(json.dumps({'inbox_rows': len(rows), 'max_id': max((r['id'] for r in rows), default=None),
                      'device_rows': {str(k): len(v) for k, v in groups.items()},
                      'identity_sha256': digest.hexdigest(), 'execute': args.execute}))
    if not args.execute:
        return
    for device_id, values in sorted(groups.items()):
        for start in range(0, len(values), 50):
            sql = f'INSERT INTO {args.database}.t_device_{device_id} VALUES ' + ' '.join(values[start:start + 50])
            command = ['docker', 'exec', 'powersystem-stage4-tdengine-1', 'sh', '-c',
                       'taos -p"$TAOS_ROOT_PASSWORD" -s "$1"', 'sh', sql]
            completed = subprocess.run(command, text=True, capture_output=True, check=True)
            result = completed.stdout + completed.stderr
            if 'Insert OK' not in result and 'Query OK' not in result:
                raise RuntimeError(f'TDengine did not confirm device {device_id} batch {start // 50}: '
                                   + result[-500:])
        print(f'device_{device_id}_replayed={len(values)}')


if __name__ == '__main__':
    main()
