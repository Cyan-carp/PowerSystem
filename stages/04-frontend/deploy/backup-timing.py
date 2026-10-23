#!/usr/bin/env python3
"""Turn the stage log written by backup.sh into one sidecar timing record.

Only durations, byte counts and stage names are stored; no command output,
paths or credentials. The record sits next to the encrypted archive so the
release controller can merge it into the publish timeline.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile

UPLOAD_STAGES = ('upload_primary', 'upload_secondary')


def read_stages(path):
    path = Path(path)
    if not path.is_file():
        return []
    stages = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get('name'), str):
            stages.append(value)
    return stages


def normalise(stages):
    rows = []
    for item in stages:
        microseconds = item.get('microseconds')
        if not isinstance(microseconds, int) or isinstance(microseconds, bool):
            continue
        if microseconds < 0:
            continue
        row = {'name': item['name'], 'seconds': round(microseconds / 1e6, 3),
               'bytes': 0}
        size = item.get('bytes')
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            row['bytes'] = size
        offset = item.get('offset_microseconds')
        if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
            row['offset'] = round(offset / 1e6, 3)
        rows.append(row)
    return rows


def uploads(rows):
    result = []
    for row in rows:
        if row['name'] not in UPLOAD_STAGES or row['seconds'] <= 0:
            continue
        megabytes = row['bytes'] / 1e6
        result.append({'name': row['name'], 'seconds': row['seconds'],
                       'bytes': row['bytes'],
                       'megabytes_per_second': round(megabytes / row['seconds'], 2)})
    return result


def summarise(stages, run_id='', status='ok', failed_stage='', failed_line='',
              archive='', staging_bytes=0, started_at='', host=''):
    rows = normalise(stages)
    archive_path = Path(archive) if archive else None
    archive_bytes = archive_path.stat().st_size if archive_path and archive_path.is_file() else 0
    total = 0.0
    for row in rows:
        total = max(total, row.get('offset', 0) + row['seconds'])
    value = {
        'schema': 1, 'phase': 'backup', 'run_id': run_id, 'status': status,
        'started_at': started_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'total_seconds': round(total, 3),
        'archive': archive_path.name if archive_path else None,
        'archive_bytes': archive_bytes,
        'staging_bytes': int(staging_bytes or 0),
        'failed_stage': failed_stage or None,
        'failed_line': failed_line or None,
        'host': host,
        'slowest': sorted(({'name': row['name'], 'seconds': row['seconds']} for row in rows),
                          key=lambda item: item['seconds'], reverse=True)[:5],
        'uploads': uploads(rows),
        'spans': rows,
    }
    return value


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.timing-')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stages', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--run-id', default='')
    parser.add_argument('--status', default='ok', choices=('ok', 'failed'))
    parser.add_argument('--failed-stage', default='')
    parser.add_argument('--failed-line', default='')
    parser.add_argument('--archive', default='')
    parser.add_argument('--staging-bytes', type=int, default=0)
    parser.add_argument('--started-at', default='')
    parser.add_argument('--host', default='')
    a = parser.parse_args(argv)
    value = summarise(read_stages(a.stages), run_id=a.run_id, status=a.status,
                      failed_stage=a.failed_stage, failed_line=a.failed_line,
                      archive=a.archive, staging_bytes=a.staging_bytes,
                      started_at=a.started_at, host=a.host)
    write(a.output, value)
    print(json.dumps({'timing': str(a.output), 'total_seconds': value['total_seconds']}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
