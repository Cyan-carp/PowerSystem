"""Make consistent SQLite snapshots of live durable queues."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


SOURCES = {
    "gateway.sqlite": Path("/source/gateway/gateway.sqlite"),
    "simulator.sqlite": Path("/source/simulator/simulator.sqlite"),
}


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: snapshot_queues.py OUTPUT_DIRECTORY")
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=False)
    for name, source in SOURCES.items():
        if not source.is_file():
            raise FileNotFoundError(source)
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as live:
            with sqlite3.connect(output / name) as snapshot:
                live.backup(snapshot)
                if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError(f"SQLite snapshot failed integrity check: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
