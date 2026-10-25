"""Upload one immutable encrypted full backup to two existing targets.

No credentials are read or printed here. SCP retains the canonical identity and
host-key checks. Each child owns a process group so interruption reaps SSH too.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def upload_copies(archive, key, targets, stages, script_start_ns):
    archive, key, stages = Path(archive), Path(key), Path(stages)
    checksum = Path(str(archive) + '.sha256')
    if not archive.is_file() or not checksum.is_file() or not key.is_file():
        raise ValueError('complete_archive_checksum_and_identity_required')
    if set(targets) != {'upload_primary', 'upload_secondary'} or len(set(targets.values())) != 2:
        raise ValueError('two_distinct_existing_targets_required')
    size = archive.stat().st_size
    jobs = []
    old_handlers = {}
    cancelled = False

    def interrupted(signum, frame):
        # Do not raise between Popen returning and registering the owned child.
        nonlocal cancelled
        cancelled = True

    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            old_handlers[name] = signal.signal(name, interrupted)
        for label, target in targets.items():
            if cancelled:
                raise InterruptedError('backup_upload_interrupted')
            offset = time.time_ns() - script_start_ns
            began = time.monotonic_ns()
            child = subprocess.Popen(
                ['scp', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                 '-i', str(key), str(archive), str(checksum), target],
                start_new_session=True)
            jobs.append({'name': label, 'child': child, 'began': began,
                         'offset': offset, 'done': False, 'code': None})
            if cancelled:
                raise InterruptedError('backup_upload_interrupted')
        while any(not job['done'] for job in jobs):
            if cancelled:
                raise InterruptedError('backup_upload_interrupted')
            for job in jobs:
                if job['done']:
                    continue
                code = job['child'].poll()
                if code is None:
                    continue
                job.update(done=True, code=code)
                row = {'name': job['name'],
                       'microseconds': (time.monotonic_ns() - job['began']) // 1000,
                       'offset_microseconds': max(0, job['offset'] // 1000),
                       'bytes': size, 'exit_code': code}
                fd = os.open(stages, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, (json.dumps(row) + '\n').encode())
                finally:
                    os.close(fd)
            if any(not job['done'] for job in jobs):
                time.sleep(0.1)
        return 0 if all(job['code'] == 0 for job in jobs) else 1
    except InterruptedError:
        return 130
    finally:
        for name in old_handlers:
            signal.signal(name, signal.SIG_IGN)
        # Only process groups created above are signalled; never enumerate or
        # terminate unrelated transfers. Reap every started child on every exit.
        for job in jobs:
            child = job['child']
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for job in jobs:
            child = job['child']
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
        for name, handler in old_handlers.items():
            signal.signal(name, handler)


def main():
    p = argparse.ArgumentParser()
    for name in ('archive', 'key', 'primary', 'secondary', 'stages'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--script-start-ns', required=True, type=int)
    a = p.parse_args()
    return upload_copies(a.archive, a.key,
                         {'upload_primary': a.primary, 'upload_secondary': a.secondary},
                         a.stages, a.script_start_ns)


if __name__ == '__main__':
    raise SystemExit(main())
