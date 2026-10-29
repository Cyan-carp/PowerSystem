"""Make consistent SQLite snapshots of live durable queues."""

from __future__ import annotations

import sqlite3
import sys
import json
import time
from contextlib import closing
from pathlib import Path


SOURCES = {
    "gateway.sqlite": Path("/source/gateway/gateway.sqlite"),
    "simulator.sqlite": Path("/source/simulator/simulator.sqlite"),
}


def snapshot_one(source, destination, deadline):
    started = time.monotonic()
    def check_budget(*_):
        if time.monotonic() >= deadline:
            raise TimeoutError('queue_snapshot_budget_exceeded')
    check_budget()
    # Pin a WAL read transaction. Live appends must not restart a multi-GB
    # incremental backup from page zero indefinitely.
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=5)) as live:
        live.execute('BEGIN')
        live.execute('SELECT rootpage FROM sqlite_schema LIMIT 1').fetchall()
        pages = live.execute('PRAGMA page_count').fetchone()[0]
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
    for name, source in SOURCES.items():
        if not source.is_file():
            raise FileNotFoundError(source)
        result = snapshot_one(source, output/name, deadline)
        print(json.dumps({'queue': name, **result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
