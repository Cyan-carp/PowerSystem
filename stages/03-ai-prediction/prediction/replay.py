"""Publish a short, clearly synthetic precursor demo into the stage-two MQTT path."""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

def sample(device: str, station: str, run: str, seq: int, ts_ms: int, onset_ms: int) -> dict[str, object]:
    # Replay uses wall-clock timestamps for the ingestion freshness checks,
    # while measurement values emulate a sunny noon from the training data.
    hour = 12.0 + (ts_ms - onset_ms) / 3_600_000
    sun = max(0.0, math.sin(math.pi * (hour - 6) / 12))
    power = 100 * sun ** 1.6 * 0.95
    voltage = 400.0 + 0.5 * math.sin(seq / 7)
    fault = onset_ms <= ts_ms < onset_ms + 10 * 60_000
    if fault:
        power *= 0.35
    current = power * 1000 / (math.sqrt(3) * voltage * 0.99)
    lead = (onset_ms - ts_ms) / 60_000
    ramp = 12 * (1 - lead / 60) if 0 < lead <= 60 else 0
    temperature = 25 + 0.25 * power + 3 * max(0.0, math.sin(math.pi * (hour - 7) / 12)) + ramp + (12 if fault else 0)
    return {
        "schema_version": 1, "run_id": run, "device_id": device, "station_id": station,
        "seq": seq, "ts_ms": ts_ms, "voltage": round(voltage, 3),
        "current": round(current, 3), "temperature": round(temperature, 3),
        "power": round(power, 3), "status": 2 if fault else (1 if power > 0.05 else 0),
        "fault_code": 1 if fault else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--device", default="INV-1001")
    parser.add_argument("--station", default="ST-01")
    parser.add_argument("--lead-minutes", type=int, default=3)
    parser.add_argument("--wait-for-fault", action="store_true")
    parser.add_argument("--full-cycle", action="store_true", help="continue through ten fault minutes and thirty recovery minutes")
    args = parser.parse_args()
    if not 1 <= args.lead_minutes <= 30:
        parser.error("lead-minutes must be 1..30")
    # The last historical point is at the start of the current minute.
    end_ms = int(time.time() // 60) * 60_000
    onset_ms = end_ms + args.lead_minutes * 60_000
    run = "stage3demo" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="stage3-demo-" + run, clean_session=True)
    client.connect(args.broker, args.port, keepalive=30)
    client.loop_start()
    try:
        for seq in range(30):
            ts_ms = end_ms - (29 - seq) * 60_000
            payload = sample(args.device, args.station, run, seq, ts_ms, onset_ms)
            client.publish("device/telemetry", json.dumps(payload), qos=1).wait_for_publish(timeout=10)
            time.sleep(0.05)
        print(json.dumps({"run_id": run, "device": args.device, "window_end_ms": end_ms,
                          "fault_onset_ms": onset_ms, "published_history": 30}, ensure_ascii=False))
        if args.wait_for_fault or args.full_cycle:
            count = 40 if args.full_cycle else 1
            for index in range(count):
                ts_ms = onset_ms + index * 60_000
                time.sleep(max(0.0, ts_ms / 1000 - time.time()))
                payload = sample(args.device, args.station, run, 30 + index, ts_ms, onset_ms)
                client.publish("device/telemetry", json.dumps(payload), qos=1).wait_for_publish(timeout=10)
            print(json.dumps({"run_id": run, "fault_published": True,
                              "recovery_points": max(0, count - 10), "fault_onset_ms": onset_ms}))
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
