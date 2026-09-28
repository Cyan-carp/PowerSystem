"""Call the running FastAPI model with a held-out synthetic precursor window."""

from __future__ import annotations

import csv
import gzip
import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "artifacts" / "stage3" / "synthetic"
BASE = "http://127.0.0.1:8090"


def main() -> None:
    health = json.load(urllib.request.urlopen(BASE + "/health", timeout=5))
    events = [json.loads(line) for line in (DATA / "fault-events.jsonl").read_text(encoding="utf-8").splitlines()]
    # A late-date event is outside the training and validation dates.
    event = events[-1]
    end = int(event["onset_ms"]) - 10 * 60_000
    first = end - 29 * 60_000
    samples = []
    with gzip.open(DATA / "telemetry.csv.gz", "rt", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            ts = int(row["ts_ms"])
            if row["device_id"] == event["device_id"] and first <= ts <= end:
                samples.append({"ts_ms": ts, **{key: float(row[key]) for key in ("voltage", "current", "temperature", "power")}})
    assert len(samples) == 30
    payload = json.dumps({"device_id": 1, "window_end_ms": end, "samples": samples}).encode()
    request = urllib.request.Request(BASE + "/predict", payload, headers={"Content-Type": "application/json"})
    result = json.load(urllib.request.urlopen(request, timeout=5))
    assert result["model_version"] == health["model_version"]
    assert result["device_id"] == 1 and result["window_end_ms"] == end
    assert 0 <= result["probability"] <= 1 and len(result["top_factors"]) == 5
    print(json.dumps({"health": health, "prediction": result}, ensure_ascii=False))
    invalid = json.dumps({"device_id": 1, "window_end_ms": end, "samples": samples[:20]}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(BASE + "/predict", invalid, headers={"Content-Type": "application/json"}), timeout=5)
        raise AssertionError("incomplete window should be rejected")
    except urllib.error.HTTPError as error:
        assert error.code == 422


if __name__ == "__main__":
    main()
