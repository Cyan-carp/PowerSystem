"""Deterministic, explicitly synthetic photovoltaic inverter telemetry."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

DEVICES = ("INV-1001", "INV-1002", "INV-1003")
CHINA_TZ = timezone(timedelta(hours=8))
STOP = threading.Event()


def log(event: str, **fields: object) -> None:
    print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields}, ensure_ascii=False), flush=True)


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS generated (device_id TEXT NOT NULL, seq INTEGER NOT NULL, ts_ms INTEGER NOT NULL, payload TEXT NOT NULL, fault_active INTEGER NOT NULL, PRIMARY KEY(device_id, seq))")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox (device_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(device_id, seq))")

    def start_ms(self) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key='start_ms'").fetchone()
            if row:
                return int(row[0])
            value = int(time.time()) * 1000
            self.db.execute("INSERT INTO meta(key,value) VALUES('start_ms',?)", (str(value),))
            return value

    def next_seq(self, device_id: str) -> int:
        with self.lock:
            row = self.db.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM generated WHERE device_id=?", (device_id,)).fetchone()
            return int(row[0])

    def add(self, device_id: str, seq: int, ts_ms: int, payload: str, fault_active: bool) -> None:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO generated VALUES (?,?,?,?,?)", (device_id, seq, ts_ms, payload, int(fault_active)))
                self.db.execute("INSERT INTO outbox VALUES (?,?,?)", (device_id, seq, payload))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def pending(self, device_id: str) -> list[tuple[int, str]]:
        with self.lock:
            return [(int(row[0]), str(row[1])) for row in self.db.execute("SELECT seq,payload FROM outbox WHERE device_id=? ORDER BY seq", (device_id,))]

    def acknowledge(self, device_id: str, seq: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM outbox WHERE device_id=? AND seq=?", (device_id, seq))

    def counts(self) -> tuple[int, int]:
        with self.lock:
            generated = int(self.db.execute("SELECT COUNT(*) FROM generated").fetchone()[0])
            pending = int(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
            return generated, pending

    def export_fault_labels(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, path.open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target)
            writer.writerow(("device_id", "seq", "ts_ms", "fault_precursor_active"))
            writer.writerows(self.db.execute("SELECT device_id,seq,ts_ms,fault_active FROM generated ORDER BY device_id,seq"))


def fault_ramp(device_id: str, instant: datetime, seed: int) -> tuple[float, bool]:
    day = instant.date().toordinal()
    dev_index = DEVICES.index(device_id)
    rng = random.Random(seed * 1_000_003 + dev_index * 1009 + day)
    starts = [10 * 60 + rng.randint(0, 90)]
    if rng.randint(0, 1):
        starts.append(14 * 60 + rng.randint(0, 90))
    minute = instant.hour * 60 + instant.minute + instant.second / 60
    for start in starts:
        if start <= minute < start + 30:
            return 15.0 * (minute - start) / 30.0, True
    return 0.0, False


def make_sample(device_id: str, seq: int, ts_ms: int, seed: int, run_id: str) -> tuple[str, bool]:
    instant = datetime.fromtimestamp(ts_ms / 1000, CHINA_TZ)
    hour = instant.hour + instant.minute / 60 + instant.second / 3600
    sun = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0))
    rng = random.Random(seed * 1_000_003 + DEVICES.index(device_id) * 1_000_000 + seq)
    cloud = 0.95 + 0.04 * math.sin(seq / 31 + DEVICES.index(device_id)) + rng.uniform(-0.015, 0.015)
    power = min(100.0, max(0.0, 100.0 * sun**1.6 * cloud))
    voltage = 400.0 + 1.5 * math.sin(seq / 71 + DEVICES.index(device_id)) + rng.uniform(-0.8, 0.8)
    current = power * 1000.0 / (math.sqrt(3) * voltage * 0.99)
    ramp, active = fault_ramp(device_id, instant, seed)
    temperature = 25.0 + 0.25 * power + 3.0 * max(0.0, math.sin(math.pi * (hour - 7.0) / 12.0)) + ramp + rng.uniform(-0.35, 0.35)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "device_id": device_id,
        "station_id": "ST-01",
        "seq": seq,
        "ts_ms": ts_ms,
        "voltage": round(voltage, 3),
        "current": round(current, 3),
        "temperature": round(temperature, 3),
        "power": round(power, 3),
        "status": 1 if power > 0.05 else 0,
        "fault_code": 0,
    }
    return json.dumps(payload, separators=(",", ":")), active


class Publisher:
    def __init__(self, device_id: str, ledger: Ledger, host: str, port: int, user: str | None, password: str | None) -> None:
        self.device_id = device_id
        self.ledger = ledger
        self.pending_mid: dict[int, int] = {}
        self.lock = threading.RLock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{device_id}", clean_session=False)
        if user:
            self.client.username_pw_set(user, password)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_publish = self.on_publish
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()

    def on_connect(self, _client: mqtt.Client, _userdata: object, _flags: object, reason_code: object, _properties: object) -> None:
        log("mqtt_connected", device_id=self.device_id, reason=str(reason_code))
        for seq, payload in self.ledger.pending(self.device_id):
            self.publish(seq, payload)

    def on_disconnect(self, _client: mqtt.Client, _userdata: object, _flags: object, reason_code: object, _properties: object) -> None:
        log("mqtt_disconnected", device_id=self.device_id, reason=str(reason_code))

    def on_publish(self, _client: mqtt.Client, _userdata: object, mid: int, _reason_code: object, _properties: object) -> None:
        with self.lock:
            seq = self.pending_mid.pop(mid, None)
            if seq is not None:
                self.ledger.acknowledge(self.device_id, seq)

    def publish(self, seq: int, payload: str) -> None:
        if not self.client.is_connected():
            return
        with self.lock:
            info = self.client.publish("device/telemetry", payload, qos=1)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                self.pending_mid[info.mid] = seq
            else:
                log("publish_deferred", device_id=self.device_id, seq=seq, code=info.rc)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--samples", type=int, default=720, help="total samples per device, including samples from a resumed run")
    parser.add_argument("--continuous", action="store_true", help="keep producing current-time samples until stopped")
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--run-id", default="manual")
    parser.add_argument("--outbox", type=Path, required=True)
    parser.add_argument("--fault-labels", type=Path, required=True)
    parser.add_argument("--mqtt-user")
    parser.add_argument("--mqtt-password")
    args = parser.parse_args()
    if args.samples < 1 or args.interval_seconds <= 0 or args.time_scale <= 0:
        parser.error("samples, interval-seconds, and time-scale must be positive")
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    ledger = Ledger(args.outbox)
    start_ms = ledger.start_ms()
    publishers = [Publisher(device, ledger, args.broker, args.port, args.mqtt_user, args.mqtt_password) for device in DEVICES]
    log("simulator_started", run_id=args.run_id, continuous=args.continuous, samples_per_device=args.samples, interval_seconds=args.interval_seconds, time_scale=args.time_scale)
    next_tick = time.monotonic()
    interrupted = False
    try:
        while not STOP.is_set() and (args.continuous or any(ledger.next_seq(device) < args.samples for device in DEVICES)):
            for publisher in publishers:
                device = publisher.device_id
                seq = ledger.next_seq(device)
                if not args.continuous and seq >= args.samples:
                    continue
                ts_ms = int(time.time() * 1000) if args.continuous else start_ms + round(seq * args.interval_seconds * 1000 * args.time_scale)
                payload, active = make_sample(device, seq, ts_ms, args.seed, args.run_id)
                ledger.add(device, seq, ts_ms, payload, active)
                publisher.publish(seq, payload)
            generated, pending = ledger.counts()
            if generated % 180 == 0:
                log("generation_progress", generated=generated, pending= pending)
            next_tick += args.interval_seconds
            STOP.wait(max(0.0, next_tick - time.monotonic()))
        interrupted = STOP.is_set()
        if not interrupted:
            deadline = time.monotonic() + 90
            while not STOP.is_set() and ledger.counts()[1] and time.monotonic() < deadline:
                STOP.wait(0.5)
    finally:
        for publisher in publishers:
            publisher.close()
        if not args.continuous:
            ledger.export_fault_labels(args.fault_labels)
        generated, pending = ledger.counts()
        log("simulator_stopped", generated=generated, pending=pending, interrupted=interrupted)
    return 130 if interrupted else (0 if pending == 0 else 2)


if __name__ == "__main__":
    sys.exit(main())
