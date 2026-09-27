"""Read-only counters for the durable simulator and gateway queues."""

import json
import sqlite3
import sys
from pathlib import Path


def count(path: Path, table: str) -> int:
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2) as db:
            return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except (sqlite3.Error, TypeError):
        return -1


run_dir = Path(sys.argv[1])
print(json.dumps({
    "generated": count(run_dir / "simulator.sqlite", "generated"),
    "simulator_pending": count(run_dir / "simulator.sqlite", "outbox"),
    "gateway_pending": count(run_dir / "gateway.sqlite", "pending"),
}))
