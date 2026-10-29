"""Make consistent SQLite snapshots of live durable queues."""

from __future__ import annotations

import sqlite3
import sys
import json
import time
from contextlib import closing
from pathlib import Path


# Mount points of the volumes that hold the queues. The file itself is resolved per
# run, because a service may move its data directory inside the same volume and leave
# the previous database behind at the volume root.
ROOTS = {
    "gateway.sqlite": Path("/source/gateway"),
    "simulator.sqlite": Path("/source/simulator"),
}


def locate(name, root):
    """The queue file the running service actually writes.

    Gateway moved its data directory inside the same volume on 2026-10-29
    (``/data`` -> ``/data/fresh-20261029-r1``) and left the previous database at the
    volume root. Trusting a fixed path either fails to open (the leftover file is WAL
    without its ``-shm``) or, worse, silently snapshots a stale queue. Take the newest
    match; on equal timestamps prefer the nested one, which is the live layout.
    """
    root = Path(root)
    candidates = [path for path in (root / name, *root.glob("*/" + name)) if path.is_file()]
    if not candidates:
        raise FileNotFoundError(root / name)
    return max(candidates, key=lambda path: (path.stat().st_mtime, len(path.parts)))


def open_pinned(source):
    """A read transaction pinned on the queue file, or a refusal to fabricate one.

    A live writer keeps ``-shm`` next to a WAL database, so ``mode=ro`` gives a
    consistent pinned view: live appends must not restart a multi-GB incremental
    backup from page zero indefinitely.

    A service that is cleanly stopped (the backup pauses telemetry so the gateway is
    down for the whole dump) leaves the same WAL-mode database behind **without**
    ``-shm``, and SQLite then refuses ``mode=ro`` with ``unable to open database
    file`` even though every byte is present. ``immutable=1`` is the documented way to
    read such a file, but it also tells SQLite to ignore a ``-wal`` file, so it is
    only used when that file is absent or empty — a non-empty ``-wal`` without
    ``-shm`` would need real WAL recovery and must not be silently truncated.
    """
    source = Path(source)
    wal = source.with_name(source.name + '-wal')
    quiesced = not (wal.exists() and wal.stat().st_size)
    candidates = [f"file:{source}?mode=ro"]
    if quiesced:
        candidates.append(f"file:{source}?mode=ro&immutable=1")
    error = None
    for uri in candidates:
        live = None
        try:
            live = sqlite3.connect(uri, uri=True, timeout=5)
            live.execute('BEGIN')
            live.execute('SELECT rootpage FROM sqlite_schema LIMIT 1').fetchall()
            return live, live.execute('PRAGMA page_count').fetchone()[0]
        except sqlite3.OperationalError as exc:
            error = exc
            if live is not None:
                live.close()
    if not quiesced:
        raise RuntimeError('queue_snapshot_wal_recovery_required') from error
    raise RuntimeError(f'queue_snapshot_unreadable: {error}')


def snapshot_one(source, destination, deadline):
    started = time.monotonic()
    def check_budget(*_):
        if time.monotonic() >= deadline:
            raise TimeoutError('queue_snapshot_budget_exceeded')
    check_budget()
    live, pages = open_pinned(source)
    with closing(live):
        with closing(sqlite3.connect(destination)) as snapshot:
            live.backup(snapshot, pages=2048, progress=check_budget, sleep=.1)
        live.rollback()
    copied = time.monotonic()
    with closing(sqlite3.connect(destination)) as snapshot:
        snapshot.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('queue_snapshot_integrity_failed')
    check_budget()
    return {'source_pages': pages, 'copy_seconds': round(copied-started, 3),
            'check_seconds': round(time.monotonic()-copied, 3)}


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: snapshot_queues.py OUTPUT_DIRECTORY")
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic()+600
    for name, root in ROOTS.items():
        source = locate(name, root)
        result = snapshot_one(source, output/name, deadline)
        print(json.dumps({'queue': name, 'source': str(source), **result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
