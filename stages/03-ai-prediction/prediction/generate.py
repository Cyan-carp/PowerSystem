"""Generate reproducible synthetic inverter days without waiting for MQTT."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CHINA = timezone(timedelta(hours=8))
DEVICES = ("INV-1001", "INV-1002", "INV-1003")
PRECURSOR_MINUTES = 60
FAULT_MINUTES = 10


def generate(out: Path, days: int, start: date, seed: int) -> dict[str, object]:
    if days < 10:
        raise ValueError("at least 10 days are needed for chronological train/validation/test")
    out.mkdir(parents=True, exist_ok=True)
    telemetry_path = out / "telemetry.csv.gz"
    events_path = out / "fault-events.jsonl"
    events: list[dict[str, object]] = []
    count = 0
    with gzip.open(telemetry_path, "wt", encoding="utf-8", newline="") as stream, events_path.open("w", encoding="utf-8") as event_file:
        writer = csv.DictWriter(stream, fieldnames=("device_id", "ts_ms", "voltage", "current", "temperature", "power", "status", "fault_code"))
        writer.writeheader()
        for day_index in range(days):
            day = start + timedelta(days=day_index)
            for device_index, device in enumerate(DEVICES):
                rng = random.Random(seed * 1_000_003 + day_index * 10_009 + device_index * 101)
                onset_minute = 11 * 60 + rng.randrange(0, 5 * 60)
                onset = datetime(day.year, day.month, day.day, tzinfo=CHINA) + timedelta(minutes=onset_minute)
                event = {
                    "event_id": f"{device}-{day.isoformat()}",
                    "device_id": device,
                    "onset_ms": int(onset.timestamp() * 1000),
                    "recovery_ms": int((onset + timedelta(minutes=FAULT_MINUTES)).timestamp() * 1000),
                    "fault_code": 1,
                    "source": "synthetic",
                }
                events.append(event)
                event_file.write(json.dumps(event, ensure_ascii=False) + "\n")
                for minute in range(24 * 60):
                    instant = datetime(day.year, day.month, day.day, tzinfo=CHINA) + timedelta(minutes=minute)
                    hour = minute / 60
                    sun = max(0.0, math.sin(math.pi * (hour - 6) / 12))
                    cloud = 0.95 + 0.04 * math.sin(minute / 31 + device_index) + rng.uniform(-0.02, 0.02)
                    power = max(0.0, min(100.0, 100 * sun ** 1.6 * cloud))
                    voltage = 400 + 1.5 * math.sin(minute / 71 + device_index) + rng.uniform(-0.8, 0.8)
                    lead = onset_minute - minute
                    ramp = 12 * (1 - lead / PRECURSOR_MINUTES) if 0 < lead <= PRECURSOR_MINUTES else 0.0
                    fault = onset_minute <= minute < onset_minute + FAULT_MINUTES
                    if fault:
                        power *= 0.35
                    current = power * 1000 / (math.sqrt(3) * voltage * 0.99)
                    temp = 25 + 0.25 * power + 3 * max(0.0, math.sin(math.pi * (hour - 7) / 12)) + ramp + (12 if fault else 0) + rng.uniform(-0.7, 0.7)
                    writer.writerow({
                        "device_id": device,
                        "ts_ms": int(instant.timestamp() * 1000),
                        "voltage": round(voltage, 3),
                        "current": round(current, 3),
                        "temperature": round(temp, 3),
                        "power": round(power, 3),
                        "status": 2 if fault else (1 if power > 0.05 else 0),
                        "fault_code": 1 if fault else 0,
                    })
                    count += 1
    manifest = {"source": "deterministic synthetic PV inverter", "days": days, "start_date": start.isoformat(), "seed": seed, "devices": list(DEVICES), "sampling_seconds": 60, "precursor_minutes": PRECURSOR_MINUTES, "fault_minutes": FAULT_MINUTES, "rows": count, "fault_events": len(events)}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 1, 1))
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    print(json.dumps(generate(args.out, args.days, args.start, args.seed), ensure_ascii=False))


if __name__ == "__main__":
    main()
