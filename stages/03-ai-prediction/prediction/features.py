"""One-minute features shared by offline training and online inference."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

FEATURE_NAMES = (
    "temperature_last",
    "temperature_mean",
    "temperature_std",
    "temperature_slope",
    "temperature_residual_last",
    "temperature_residual_slope",
    "temperature_rising_minutes",
    "power_last",
    "power_mean",
    "power_std",
    "power_slope",
    "voltage_mean",
    "voltage_std",
    "current_mean",
    "current_std",
)
WINDOW_MINUTES = 30
MIN_PRESENT_MINUTES = 27


def _stats(values: list[float]) -> tuple[float, float]:
    mean = sum(values) / len(values)
    return mean, math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))


def extract(samples: Sequence[Mapping[str, object]], end_ms: int) -> dict[str, float]:
    """Use only measurements at or before end_ms. Raise on incomplete windows."""
    first_bucket = end_ms // 60_000 - (WINDOW_MINUTES - 1)
    last_bucket = end_ms // 60_000
    by_minute: dict[int, Mapping[str, object]] = {}
    for sample in samples:
        ts = int(sample["ts_ms"])
        bucket = ts // 60_000
        if ts <= end_ms and first_bucket <= bucket <= last_bucket:
            old = by_minute.get(bucket)
            if old is None or int(old["ts_ms"]) < ts:
                by_minute[bucket] = sample
    if len(by_minute) < MIN_PRESENT_MINUTES:
        raise ValueError("fewer than 27 of 30 minutes have data")
    buckets = sorted(by_minute)
    if buckets[-1] != last_bucket or any(b - a > 2 for a, b in zip(buckets, buckets[1:])):
        raise ValueError("telemetry window is stale or has a long gap")
    points = [by_minute[b] for b in buckets]
    values: dict[str, list[float]] = {}
    for name in ("temperature", "power", "voltage", "current"):
        numbers = [float(p[name]) for p in points]
        if any(not math.isfinite(n) for n in numbers):
            raise ValueError("non-finite telemetry")
        values[name] = numbers
    temp = values["temperature"]
    power = values["power"]
    residual = [t - 0.25 * p for t, p in zip(temp, power)]
    span = buckets[-1] - buckets[0]
    tm, ts = _stats(temp)
    pm, ps = _stats(power)
    vm, vs = _stats(values["voltage"])
    cm, cs = _stats(values["current"])
    return {
        "temperature_last": temp[-1],
        "temperature_mean": tm,
        "temperature_std": ts,
        "temperature_slope": (temp[-1] - temp[0]) / span,
        "temperature_residual_last": residual[-1],
        "temperature_residual_slope": (residual[-1] - residual[0]) / span,
        "temperature_rising_minutes": float(sum(b > a for a, b in zip(residual, residual[1:]))),
        "power_last": power[-1],
        "power_mean": pm,
        "power_std": ps,
        "power_slope": (power[-1] - power[0]) / span,
        "voltage_mean": vm,
        "voltage_std": vs,
        "current_mean": cm,
        "current_std": cs,
    }
