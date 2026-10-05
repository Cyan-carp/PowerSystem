"""One writer, fixed images/restart baseline, complete 30-minute observation."""
import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SERVICES = ('api', 'agent', 'frontend')


def inspect_services():
    raw = subprocess.check_output(['docker', 'inspect', *['powersystem-stage4-' + s + '-1' for s in SERVICES]], text=True)
    rows = json.loads(raw)
    return {name: {'running': row['State']['Running'],
                  'health': row['State'].get('Health', {}).get('Status', 'none'),
                  'restart_count': row['RestartCount'], 'image': row['Image']}
            for name, row in zip(SERVICES, rows)}


def service_checks(states, baseline):
    return (set(states) == set(SERVICES) and set(baseline) == set(SERVICES)
            and all(states[s]['running'] and states[s]['health'] == 'healthy'
                    and states[s]['image'] == baseline[s]['image']
                    and states[s]['restart_count'] == baseline[s]['restart_count'] for s in SERVICES))


def status(url, path):
    before = time.monotonic()
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + path, timeout=10) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        code = error.code
    except OSError:
        code = 0
    return {'code': code, 'milliseconds': round((time.monotonic() - before) * 1000)}


def main(seconds, output, url, images=None, *, inspector=inspect_services,
         requester=status, monotonic=time.monotonic, sleep=time.sleep):
    if not 1800 <= seconds <= 7200:
        raise ValueError('observation duration must be 30-120 minutes')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError('observation output already exists')
    lock = output.with_suffix(output.suffix + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    samples = []
    start = datetime.now(timezone.utc).isoformat()
    started = monotonic()
    report = {'started_at': start, 'duration_seconds': seconds, 'url': url,
              'completed': False, 'passed': False, 'samples': samples}

    def save():
        temporary = output.with_suffix(output.suffix + '.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(output)

    try:
        baseline = inspector()
        if images is not None:
            if set(images) != set(SERVICES) or any(baseline[s]['image'] != images[s] for s in SERVICES):
                raise ValueError('release images do not match running services')
        report['baseline'] = baseline
        save()
        while True:
            sample = {'time': datetime.now(timezone.utc).isoformat()}
            sample.update({key: requester(url, path) for key, path in (
                ('ping', '/api/v1/ping'), ('internal', '/internal/agent/model-status'),
                ('JWT', '/api/v1/agent/interpretations'))})
            sample['services'] = inspector()
            sample['passed'] = (sample['ping']['code'] == 200 and sample['internal']['code'] == 403
                                and sample['JWT']['code'] == 401 and service_checks(sample['services'], baseline))
            samples.append(sample)
            elapsed = monotonic() - started
            report.update(elapsed_seconds=round(elapsed, 2), completed=elapsed >= seconds,
                          passed=elapsed >= seconds and all(item['passed'] for item in samples))
            save()
            if report['completed']:
                break
            sleep(min(30, seconds - elapsed))
    except BaseException as error:
        report.update(passed=False, error=type(error).__name__, elapsed_seconds=round(monotonic() - started, 2))
        save()
        raise
    finally:
        lock.unlink(missing_ok=True)
    print('Observation complete:', len(samples), 'samples; passed=', report['passed'])
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--images', type=Path, help='current-images.json from this release')
    args = parser.parse_args()
    expected = json.loads(args.images.read_text()) if args.images else None
    raise SystemExit(0 if main(args.seconds, args.output, args.url.rstrip('/'), expected) else 1)
