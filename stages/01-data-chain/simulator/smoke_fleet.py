"""Run the real simulator loop with seven devices and offline MQTT receipts."""

from __future__ import annotations

import argparse
import json
import queue
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import simulator
from fleet import expand_devices, load_fleet


class OfflineClient:
    """Queue PUBACKs asynchronously; never open a socket or import paho."""

    def __init__(self) -> None:
        self.receipts: queue.Queue[int] = queue.Queue()
        self.stopped = threading.Event()
        self.mid = 0
        self.sent = 0

    def connect_async(self, host, port, keepalive):
        if host != "offline.invalid":
            raise ValueError("offline smoke requires the reserved offline host")

    def loop_start(self):
        def pump():
            self.on_connect(self, None, None, 0)
            while not self.stopped.is_set() or not self.receipts.empty():
                try:
                    mid = self.receipts.get(timeout=0.05)
                except queue.Empty:
                    continue
                self.on_publish(self, None, mid, 0)

        self.thread = threading.Thread(target=pump, daemon=True)
        self.thread.start()

    def publish(self, topic, payload, qos):
        if topic != "device/telemetry" or qos != 1:
            raise ValueError("unexpected telemetry publish contract")
        self.mid += 1
        self.sent += 1
        self.receipts.put(self.mid)
        return SimpleNamespace(rc=0, mid=self.mid)

    def loop_stop(self):
        self.stopped.set()
        self.thread.join(timeout=2)

    def disconnect(self):
        self.on_disconnect(self, None, None, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fleet", type=Path, default=Path(__file__).parent / "fixtures/fleet-smoke-7.json")
    parser.add_argument("--output", type=Path, required=True, help="new, isolated evidence directory")
    args = parser.parse_args()
    rows = expand_devices(load_fleet(args.fleet))
    if len(rows) != 7 or sum(row["scope"] == "business" for row in rows) != 4:
        parser.error("use the seven-device smoke fixture")
    args.output.mkdir(parents=True, exist_ok=False)
    ledger_path = args.output / "outbox.sqlite"
    client = OfflineClient()
    publisher_type = simulator.SharedPublisher
    argv = ["simulator.py", "--fleet", str(args.fleet.resolve()),
            "--broker", "offline.invalid", "--samples", "24", "--interval-seconds", "5",
            "--run-id", "fleet-minimal-offline", "--outbox", str(ledger_path),
            "--fault-labels", str(args.output / "fault-labels.csv")]
    simulator.STOP.clear()
    started = time.monotonic()
    with patch.object(sys, "argv", argv), patch.object(
        simulator, "SharedPublisher", lambda ledger, **kw: publisher_type(ledger, client=client, **kw)
    ):
        result = simulator.main()
    with sqlite3.connect(ledger_path) as db:
        counts = dict(db.execute("SELECT device_id, count(*) FROM generated GROUP BY device_id"))
        pending = db.execute("SELECT count(*) FROM outbox").fetchone()[0]
        identity_errors = 0
        for device, seq, raw in db.execute("SELECT device_id, seq, payload FROM generated"):
            payload = json.loads(raw)
            if payload["device_id"] != device or payload["seq"] != seq:
                identity_errors += 1
    passed = result == 0 and pending == 0 and identity_errors == 0 and counts == {
        row["device_code"]: 24 for row in rows
    }
    report = {"mode": "offline-receipts-real-simulator-loop", "passed": passed,
              "elapsed_seconds": round(time.monotonic() - started, 2),
              "devices": counts, "generated": sum(counts.values()), "published": client.sent,
              "pending": pending, "payload_identity_errors": identity_errors,
              "real_broker": False}
    (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
