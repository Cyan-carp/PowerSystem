"""Generate reproducible synthetic inverter days without waiting for MQTT.

两种故障形态：

- 默认（``window_mode=False``）：渐进升温型，故障前 60 分钟有温度斜坡前兆；
- ``window_mode=True``：复刻生产模拟器 ``simulator.py`` 的窗口注入——固定 5 个窗口，
  每窗口 10 分钟，窗口内把某一指标直接抬到目标值（``max(当前值, target)``），
  **窗口前无任何前兆**，与线上 ``apply_window_injection`` 的行为一致。

改造动机：线上注入是突变型，而历史训练集是渐进型，模型学的是「病前征兆」，
对突变注入零命中（见部署日志 76）。用窗口模式重新造训练集，数据分布才与线上对齐。
"""

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

# 与 stages/01-data-chain/simulator/simulator.py 的 WINDOW_SCENARIOS 保持一致：
# 时刻、指标、注入目标值。顺序即槽位，用来按天轮换设备。
WINDOW_SCENARIOS = (
    (9, "temperature", 65.0),
    (11, "voltage", 445.0),
    (13, "temperature", 82.0),
    (15, "temperature", 65.0),
    (17, "temperature", 82.0),
)
WINDOW_MINUTES = 10


def generate(out: Path, days: int, start: date, seed: int, window_mode: bool = False) -> dict[str, object]:
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
            midnight = datetime(day.year, day.month, day.day, tzinfo=CHINA)

            # 窗口模式下先排好当天 5 个窗口；每窗口轮换一台设备，同一设备当天可能落两个窗口
            windows: list[dict[str, object]] = []
            if window_mode:
                for slot, (hour, metric, target) in enumerate(WINDOW_SCENARIOS):
                    device = DEVICES[(day_index + slot) % len(DEVICES)]
                    begin = midnight + timedelta(hours=hour)
                    windows.append({
                        "device_id": device,
                        "metric": metric,
                        "target": target,
                        "start_minute": hour * 60,
                        "end_minute": hour * 60 + WINDOW_MINUTES,
                        "onset": begin,
                    })
                    event = {
                        "event_id": f"{device}-{day.isoformat()}-{hour:02d}",
                        "device_id": device,
                        "onset_ms": int(begin.timestamp() * 1000),
                        "recovery_ms": int((begin + timedelta(minutes=WINDOW_MINUTES)).timestamp() * 1000),
                        "fault_code": 1,
                        "source": "window_synthetic",
                        "metric": metric,
                        "target": target,
                    }
                    events.append(event)
                    event_file.write(json.dumps(event, ensure_ascii=False) + "\n")

            for device_index, device in enumerate(DEVICES):
                rng = random.Random(seed * 1_000_003 + day_index * 10_009 + device_index * 101)
                if window_mode:
                    device_windows = [w for w in windows if w["device_id"] == device]
                    onset_minute = -1
                else:
                    onset_minute = 11 * 60 + rng.randrange(0, 5 * 60)
                    onset = midnight + timedelta(minutes=onset_minute)
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
                    instant = midnight + timedelta(minutes=minute)
                    hour = minute / 60
                    sun = max(0.0, math.sin(math.pi * (hour - 6) / 12))
                    cloud = 0.95 + 0.04 * math.sin(minute / 31 + device_index) + rng.uniform(-0.02, 0.02)
                    power = max(0.0, min(100.0, 100 * sun ** 1.6 * cloud))
                    voltage = 400 + 1.5 * math.sin(minute / 71 + device_index) + rng.uniform(-0.8, 0.8)
                    ramp = 0.0
                    fault = False

                    if window_mode:
                        hit = next(
                            (w for w in device_windows if w["start_minute"] <= minute < w["end_minute"]),
                            None,
                        )
                        if hit is not None:
                            fault = True
                            if hit["metric"] == "temperature":
                                # 生产侧是 max(当前值, target)，这里同样只抬不压
                                pass
                            elif hit["metric"] == "voltage":
                                voltage = max(voltage, float(hit["target"]))
                    else:
                        lead = onset_minute - minute
                        ramp = 12 * (1 - lead / PRECURSOR_MINUTES) if 0 < lead <= PRECURSOR_MINUTES else 0.0
                        fault = onset_minute <= minute < onset_minute + FAULT_MINUTES
                        if fault:
                            power *= 0.35

                    current = power * 1000 / (math.sqrt(3) * voltage * 0.99)
                    temp = 25 + 0.25 * power + 3 * max(0.0, math.sin(math.pi * (hour - 7) / 12)) + ramp + rng.uniform(-0.7, 0.7)

                    if window_mode and hit is not None:
                        if hit["metric"] == "temperature":
                            temp = max(temp, float(hit["target"]))
                        # 电压窗口只抬电压，电流已按新的电压重算，温度不动

                    status = 2 if fault else (1 if power > 0.05 else 0)
                    writer.writerow({
                        "device_id": device,
                        "ts_ms": int(instant.timestamp() * 1000),
                        "voltage": round(voltage, 3),
                        "current": round(current, 3),
                        "temperature": round(temp, 3),
                        "power": round(power, 3),
                        "status": status,
                        "fault_code": 1 if fault else 0,
                    })
                    count += 1
    manifest = {
        "source": "deterministic synthetic PV inverter",
        "days": days,
        "start_date": start.isoformat(),
        "seed": seed,
        "devices": list(DEVICES),
        "sampling_seconds": 60,
        "fault_mode": "window_injection" if window_mode else "precursor_ramp",
        "precursor_minutes": 0 if window_mode else PRECURSOR_MINUTES,
        "fault_minutes": WINDOW_MINUTES if window_mode else FAULT_MINUTES,
        "window_scenarios": (
            [{"hour": h, "metric": m, "target": t} for h, m, t in WINDOW_SCENARIOS] if window_mode else []
        ),
        "rows": count,
        "fault_events": len(events),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 1, 1))
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--window-mode", action="store_true", help="按生产窗口注入生成突变型故障")
    args = parser.parse_args()
    print(json.dumps(
        generate(args.out, args.days, args.start, args.seed, window_mode=args.window_mode),
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
