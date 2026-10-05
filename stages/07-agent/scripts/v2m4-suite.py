"""Repeatable local release checks. All reports belong to one new private run."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]


def main(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'suite.json').exists():
        raise FileExistsError('use a new run directory')
    version = subprocess.check_output([str(args.node), '--version'], text=True).strip()
    if int(version.split('.')[0][1:]) < 24:
        raise ValueError('use the existing Node 24 runtime')
    env = dict(os.environ, GOTELEMETRY='off', V2M4_TEST_REDIS_URL='redis://127.0.0.1:16379/0')
    deps = ROOT / 'artifacts/stage7-智能体/pydeps'
    if not deps.is_dir(): deps = ROOT / 'artifacts/stage7/pydeps'
    env['PYTHONPATH'] = str(deps) + os.pathsep + str(ROOT / 'stages/07-agent')
    rows = []
    metadata = {'started_at': datetime.now(timezone.utc).isoformat(), 'node': version,
                'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'completed': False, 'passed': False, 'cases': rows}
    def save(): (output / 'suite.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    container = None
    try:
        container = subprocess.check_output(['docker', 'run', '-d', '--rm', '--label', 'powersystem.v2m4=disposable',
            '--name', 'powersystem-v2m4-redis-' + uuid4().hex[:8], '-p', '127.0.0.1:16379:6379',
            'redis:7.4.5-alpine'], text=True).strip()
        for _ in range(30):
            if subprocess.run(['docker', 'exec', container, 'redis-cli', 'ping'], capture_output=True).stdout.strip() == b'PONG': break
            time.sleep(.2)
        for name, cwd, command in [
            ('agent-offline', ROOT, [sys.executable, '-X', 'utf8', '-m', 'unittest', 'discover', '-s', 'stages/07-agent/tests', '-v']),
            ('go-all-with-live-limiter', ROOT / 'stages/02-backend', ['go', 'test', '-count=1', './...']),
            ('frontend-tests', ROOT / 'stages/04-frontend', [str(args.node), str(args.npm_cli), 'test', '--', '--reporter=dot']),
            ('frontend-build', ROOT / 'stages/04-frontend', [str(args.node), str(args.npm_cli), 'run', 'build'])]:
            started = time.monotonic()
            with (output / (name + '.log')).open('w', encoding='utf-8') as log:
                result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
            rows.append({'case': name, 'result': 'passed' if result.returncode == 0 else 'failed',
                         'exit_code': result.returncode, 'seconds': round(time.monotonic()-started, 3), 'evidence': name+'.log'})
            save()
            if result.returncode: return False
        metadata.update(completed=True, passed=True)
        paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        hashes = {rel: hashlib.sha256((ROOT/rel).read_bytes()).hexdigest() for rel in paths
                  if rel and (ROOT/rel).is_file() and not rel.startswith('.obsidian/')}
        (output/'source-hashes.json').write_text(json.dumps(hashes,ensure_ascii=False,indent=2),encoding='utf-8')
        return True
    except BaseException as error:
        metadata['error'] = type(error).__name__
        raise
    finally:
        if container:
            result = subprocess.run(['docker', 'stop', container], capture_output=True)
            metadata['disposable_redis_stopped'] = result.returncode == 0
            if result.returncode:
                metadata['passed'] = False
                save()
                raise RuntimeError('disposable Redis cleanup failed')
        save()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--npm-cli', type=Path, required=True)
    args=parser.parse_args()
    raise SystemExit(0 if main(args) else 1)
