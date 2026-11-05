"""Generate reproducible synthetic inverter days without waiting for MQTT.

两种故障形态：

- 默认（``window_mode=False``）：渐进升温型，故障前 60 分钟有温度斜坡前兆；
- ``window_mode=True``：复刻生产模拟器 ``simulator.py`` 的窗口注入——固定 5 个窗口，
  每窗口 10 分钟，窗口内把某一指标直接抬到目标值（``max(当前值, target)``）；
  注入前 ``WINDOW_PRECURSOR_MINUTES=60`` 分钟有合成前兆（指标向目标缓升、标签仍为正常），
  与线上 ``apply_window_injection`` 的行为一致。

改造动机：线上注入在窗口内是突变型、且窗口前 60 分钟有合成前兆，而历史训练集
（``window_mode=False``）是渐进升温型，模型学的是「病前征兆」的斜率，对突变注入零命中
（见部署日志 76）。用窗口模式重新造训练集，数据分布才与线上对齐。
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
# 生产里 143 台设备的效率与安装位置都不同，同一天同一时刻温度能差十几度。
# 早期生成器让三台设备用同一套 sun/cloud，温度只差噪声——模型因此从没学过
# 「功率高所以热是正常的」，上线后看到整站升温就给整站报警。
# 下面两个系数把设备差异补回来：效率差影响功率，安装位置差影响温升。
DEVICE_EFFICIENCY = {"INV-1001": 1.00, "INV-1002": 0.93, "INV-1003": 0.86}
DEVICE_TEMP_OFFSET = {"INV-1001": 0.0, "INV-1002": 1.3, "INV-1003": -1.2}
PRECURSOR_MINUTES = 60
FAULT_MINUTES = 10

# 与 stages/01-data-chain/simulator/simulator.py 的 WINDOW_SCENARIOS 保持一致：
# 时刻、指标、注入目标值。顺序即槽位，用来按天轮换设备。
WINDOW_SCENARIOS = (
    # 与 simulator.py 的 WINDOW_SCENARIOS 必须逐项一致：训练数据的温度目标
    # 只要和线上差一度，模型学到的东西就与线上形态对不上。
    (9, "temperature", 80.0),
    (11, "voltage", 445.0),
    (13, "temperature", 82.0),
    (15, "temperature", 80.0),
    (17, "temperature", 82.0),
)
WINDOW_MINUTES = 10
# 前兆时长与爬升比例，必须与 simulator.py 的 --precursor-minutes / WINDOW_PRECURSOR_RATIO 一致，
# 否则训练形态与线上形态仍然对不上（这是 2026-11-02 首轮"窗口训练"效果差的根因）。
WINDOW_PRECURSOR_MINUTES = 60
WINDOW_PRECURSOR_RATIO = 0.7

# 正常电压波动多样化训练模式（2026-11-04 新增，可复现）：
# 默认合成生成器的电压只做 ±0.8 均匀抖动，单窗 voltage_std 远小于真实场站的
# 约 0.98–1.17（中位）/1.30（p90）。试用版 20a748 把正常水平的 voltage_std 误判为故障
# （见模型质量第三批有界对照 n=20），根因是训练数据从未出现「正常但 voltage_std 偏高」的样本。
# 该模式让无故障样本的电压抖动按设备/天取确定性 sigma∈[VOLTAGE_SIGMA_MIN, VOLTAGE_SIGMA_MAX]
# （含 1.1–1.3），使模型学到「voltage_std 高到 1.5 仍可能是正常」。故障注入语义不变：
# 电压故障仍把电压抬到 445，单窗 std 远超该区间，仍可区分。
VOLTAGE_SIGMA_MIN = 0.8
VOLTAGE_SIGMA_MAX = 1.5


def _device_day_sigma(seed: int, day_index: int, device_index: int) -> float:
    """每台设备每天的确定性电压抖动幅度，落在 [VOLTAGE_SIGMA_MIN, VOLTAGE_SIGMA_MAX]。

    用与逐分钟 rng 独立的种子，保证 sigma 只由 (seed, 设备, 天) 决定、可复现，
    且不受循环内其他随机调用顺序影响。
    """
    rng = random.Random(seed * 31 + device_index * 1009 + day_index * 7919)
    return VOLTAGE_SIGMA_MIN + (VOLTAGE_SIGMA_MAX - VOLTAGE_SIGMA_MIN) * rng.random()


def _base_voltage(
    minute: int,
    device_index: int,
    rng: random.Random,
    normal_voltage_diverse: bool,
    seed: int,
    day_index: int,
) -> float:
    """逐分钟电压基值。

    normal_voltage_diverse=True 时做真实化抖动；否则保持旧公式（与旧生成模式逐字节一致、可复现）。
    故障分钟的电压随后会被 shape_value 覆盖为注入目标，本函数只决定非故障电压。
    """
    if normal_voltage_diverse:
        sigma = _device_day_sigma(seed, day_index, device_index)
        # 每分钟独立零均值噪声：uniform(-a, a) 的总体 std = a/√3，取 a = sigma*√3 即总体 std≈sigma；
        # 30 分钟窗口的样本 std 会围绕 sigma 小幅波动（约 ±0.13·sigma），足以覆盖 0.8–1.5 全区间。
        # 再叠一个很小的慢漂移仅用于真实感，不改变窗口 std 的量级。
        noise = sigma * math.sqrt(3) * rng.uniform(-1.0, 1.0)
        drift = 0.2 * sigma * math.sin(minute / 71.0 + device_index)
        return 400.0 + drift + noise
    return 400 + 1.5 * math.sin(minute / 71 + device_index) + rng.uniform(-0.8, 0.8)


def shape_value(base: float, target: float, ratio: float) -> float:
    """窗口内取 max(base, target)；前兆期按比例插值，且不超过目标值。"""
    if ratio >= 1.0:
        return max(base, target)
    if base >= target:
        return base
    return min(target, base + (target - base) * ratio * WINDOW_PRECURSOR_RATIO)


def generate(
    out: Path,
    days: int,
    start: date,
    seed: int,
    window_mode: bool = False,
    normal_voltage_diverse: bool = False,
) -> dict[str, object]:
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
                    efficiency = DEVICE_EFFICIENCY.get(device, 1.0)
                    power = max(0.0, min(100.0, 100 * sun ** 1.6 * cloud * efficiency))
                    voltage = _base_voltage(minute, device_index, rng, normal_voltage_diverse, seed, day_index)
                    ramp = 0.0
                    fault = False

                    if window_mode:
                        hit = None
                        ratio = 0.0
                        for w in device_windows:
                            if w["start_minute"] <= minute < w["end_minute"]:
                                hit, ratio = w, 1.0
                                break
                            window_start = w["start_minute"]
                            if WINDOW_PRECURSOR_MINUTES > 0 and window_start - WINDOW_PRECURSOR_MINUTES <= minute < window_start:
                                hit = w
                                ratio = 1.0 - (window_start - minute) / WINDOW_PRECURSOR_MINUTES
                                break
                        if hit is not None:
                            # 只有进入窗口才算故障；前兆期是"即将发生"，标签仍为正常
                            fault = ratio >= 1.0
                            if hit["metric"] == "voltage":
                                voltage = shape_value(voltage, float(hit["target"]), ratio)
                    else:
                        lead = onset_minute - minute
                        ramp = 12 * (1 - lead / PRECURSOR_MINUTES) if 0 < lead <= PRECURSOR_MINUTES else 0.0
                        fault = onset_minute <= minute < onset_minute + FAULT_MINUTES
                        if fault:
                            power *= 0.35

                    current = power * 1000 / (math.sqrt(3) * voltage * 0.99)
                    temp = (25 + 0.25 * power + 3 * max(0.0, math.sin(math.pi * (hour - 7) / 12))
                            + DEVICE_TEMP_OFFSET.get(device, 0.0) + ramp + rng.uniform(-0.7, 0.7))

                    if window_mode and hit is not None and hit["metric"] == "temperature":
                        temp = shape_value(temp, float(hit["target"]), ratio)
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
        "precursor_minutes": WINDOW_PRECURSOR_MINUTES if window_mode else PRECURSOR_MINUTES,
        "fault_minutes": WINDOW_MINUTES if window_mode else FAULT_MINUTES,
        "normal_voltage_diverse": normal_voltage_diverse,
        "voltage_sigma_range": [VOLTAGE_SIGMA_MIN, VOLTAGE_SIGMA_MAX] if normal_voltage_diverse else None,
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
    parser.add_argument("--window-mode", action="store_true", help="按生产窗口注入生成突变型故障（注入前 60 分钟合成前兆，前兆期标签仍正常）")
    parser.add_argument(
        "--normal-voltage-diverse",
        action="store_true",
        help="正常样本按设备/天取确定性 voltage_std∈[0.8,1.5]（含1.1-1.3），修复正常波动被误判为故障的训练偏差；故障注入语义不变",
    )
    args = parser.parse_args()
    print(json.dumps(
        generate(
            args.out,
            args.days,
            args.start,
            args.seed,
            window_mode=args.window_mode,
            normal_voltage_diverse=args.normal_voltage_diverse,
        ),
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
