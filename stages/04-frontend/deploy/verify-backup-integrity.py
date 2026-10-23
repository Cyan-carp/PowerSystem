#!/usr/bin/env python3
"""Read-only integrity check for a PowerSystem full backup.

This is the "light continuity check" that runs whenever a fast baseline is
renewed. It proves the ciphertext is intact *and* that its contents can be
opened, listed and measured, without starting an isolated stack:

* sidecar SHA256 of the encrypted archive
* GPG decryption (the stored passphrase still opens the archive)
* inner SHA256SUMS over every frozen file
* required private files, model metadata, queue snapshots and engine outputs
* PostgreSQL TOC readability through ``pg_restore -l`` (no database is created)
* SQLite queue snapshots opened read-only, tables and rows counted

It is deliberately narrower than ``restore-verify.sh``: no containers of the
production compose are started, no data is imported, and its success must not
be recorded as a completed restore drill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

BACKUP_DIR = Path('/var/backups/powersystem')
PASSPHRASE = Path('/etc/powersystem/backup-passphrase')
ARCHIVE = re.compile(r'powersystem-\d{8}T\d{6}Z\.tar\.gz\.gpg')
POSTGRES_IMAGE = 'postgres:17.6'

REQUIRED_FILES = (
    'config/.env',
    'config/feishu-webhook-url',
    'config/feishu-sign-secret',
    'config/business-notify-token',
    'model.json',
    'metadata.json',
    'postgres.dump',
    'redis.rdb',
)


class Failed(Exception):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise Failed(code)


def note(message: str) -> None:
    """Progress goes to stderr so stdout stays a single JSON document."""
    print(message, file=sys.stderr, flush=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(argv: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def decrypt(archive: Path, target: Path) -> None:
    result = run(['gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
                  '--passphrase-file', str(PASSPHRASE),
                  '--decrypt', '--output', str(target), str(archive)])
    require(result.returncode == 0, 'decrypt_failed:' + (result.stderr or '').strip()[:120])


def extract(archive: Path, destination: Path) -> None:
    result = run(['tar', '-C', str(destination), '-xzf', str(archive)])
    require(result.returncode == 0, 'extract_failed:' + (result.stderr or '').strip()[:120])


def verify_sums(root: Path) -> int:
    manifest = root / 'SHA256SUMS'
    require(manifest.is_file(), 'sha256sums_missing')
    checked = 0
    for line in manifest.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        expected, _, name = line.partition('  ')
        target = root / name.lstrip('./')
        require(target.is_file(), 'listed_file_missing:' + name)
        require(sha256(target) == expected, 'listed_file_changed:' + name)
        checked += 1
    return checked


def postgres_toc(root: Path) -> dict[str, int]:
    dump = root / 'postgres.dump'
    result = run(['docker', 'run', '--rm', '--entrypoint', 'pg_restore',
                  '-v', f'{root}:/d:ro', POSTGRES_IMAGE, '-l', '/d/postgres.dump'], timeout=300)
    require(result.returncode == 0, 'pg_restore_list_failed:' + (result.stderr or '').strip()[:120])
    tables = data = 0
    for line in result.stdout.splitlines():
        if line.startswith(';'):
            continue
        if ' TABLE DATA ' in line:
            data += 1
        elif ' TABLE ' in line:
            tables += 1
    require(tables > 0 and data > 0, 'pg_restore_toc_empty')
    return {'tables': tables, 'table_data': data}


def queue_snapshots(root: Path) -> list[dict[str, object]]:
    files = sorted((root / 'queues').glob('*.sqlite'))
    require(len(files) >= 2, 'queue_snapshots_missing')
    rows = []
    for path in files:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            names = [row[0] for row in
                     con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            counts = {}
            for name in names:
                counts[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            rows.append({'file': path.name, 'tables': len(names), 'rows': counts})
        finally:
            con.close()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive')
    parser.add_argument('--output', type=Path, help='write the JSON report here')
    parser.add_argument('--skip-toc', action='store_true',
                        help='skip the pg_restore -l readability check (no docker needed)')
    parser.add_argument('--keep', action='store_true', help='keep the extracted staging copy')
    args = parser.parse_args()

    archive = Path(args.archive).resolve()
    report: dict[str, object] = {'archive': archive.name, 'checks': {}, 'seconds': {}}
    started = time.time()
    staging: Path | None = None
    try:
        require(archive.parent == BACKUP_DIR and bool(ARCHIVE.fullmatch(archive.name)),
                'archive_must_be_full_backup_in_backup_dir')
        require(archive.is_file(), 'archive_missing')
        require(PASSPHRASE.is_file(), 'passphrase_missing')

        mark = time.time()
        expected = Path(str(archive) + '.sha256').read_text().split()[0]
        actual = sha256(archive)
        require(actual == expected, 'archive_checksum_mismatch')
        report['checks']['archive_sha256'] = actual
        report['seconds']['archive_sha256'] = round(time.time() - mark, 1)
        note('archive_sha256=ok')

        mark = time.time()
        staging = Path(tempfile.mkdtemp(prefix='powersystem-integrity-'))
        decrypt(archive, staging / 'archive.tar.gz')
        report['seconds']['decrypt'] = round(time.time() - mark, 1)
        note('decrypt=ok')

        mark = time.time()
        extract(staging / 'archive.tar.gz', staging)
        (staging / 'archive.tar.gz').unlink(missing_ok=True)
        run_id = archive.name[:-len('.tar.gz.gpg')]
        root = staging / run_id
        require(root.is_dir(), 'archive_layout_unexpected')
        count = verify_sums(root)
        report['checks']['files_verified'] = count
        report['seconds']['extract_and_hash'] = round(time.time() - mark, 1)
        note(f'inner_files_verified={count}')

        for name in REQUIRED_FILES:
            require((root / name).is_file() and (root / name).stat().st_size > 0,
                    'required_file_missing:' + name)
        require(any((root / 'tdengine').iterdir()), 'tdengine_dump_empty')
        report['checks']['required_files'] = len(REQUIRED_FILES) + 1

        if not args.skip_toc:
            mark = time.time()
            report['checks']['postgres_toc'] = postgres_toc(root)
            report['seconds']['pg_restore_list'] = round(time.time() - mark, 1)
            note(f"postgres_toc={report['checks']['postgres_toc']}")

        mark = time.time()
        report['checks']['queues'] = queue_snapshots(root)
        report['seconds']['queues'] = round(time.time() - mark, 1)

        report['status'] = 'ok'
        report['note'] = ('integrity only: no service started and no data imported; '
                          'this does not count as a restore drill')
        return_code = 0
    except (Failed, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        return_code = 1
    finally:
        if staging is not None and not args.keep:
            shutil.rmtree(staging, ignore_errors=True)
        report['seconds']['total'] = round(time.time() - started, 1)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        text = json.dumps(report, ensure_ascii=False, indent=2)
        print(text)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + '\n', encoding='utf-8')

    return return_code


if __name__ == '__main__':
    sys.exit(main())
