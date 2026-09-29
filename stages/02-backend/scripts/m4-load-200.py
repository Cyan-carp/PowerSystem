"""Publish 200 synthetic devices every five seconds in an isolated M4 stack."""

import argparse
import json
import time

import paho.mqtt.client as mqtt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default="emqx")
    parser.add_argument("--cycles", type=int, default=60)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.cycles < 1 or args.cycles > 720:
        parser.error("cycles must be between 1 and 720")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="m4-load-200", clean_session=False)
    client.connect(args.broker, 1883, keepalive=30)
    client.loop_start()
    started = time.monotonic()
    published = 0
    late_cycles = 0
    max_cycle_seconds = 0.0
    try:
        for cycle in range(args.cycles):
            scheduled = started + cycle * 5
            time.sleep(max(0, scheduled - time.monotonic()))
            cycle_started = time.monotonic()
            ts_ms = int(time.time() * 1000)
            pending = []
            for number in range(1, 201):
                device = f"LOAD-{number:04d}"
                payload = {
                    "schema_version": 1, "run_id": args.run_id,
                    "device_id": device, "station_id": "ST-LOAD", "seq": cycle,
                    "ts_ms": ts_ms, "voltage": 400.0, "current": 20.0,
                    "temperature": 30.0, "power": 13.719, "status": 1, "fault_code": 0,
                }
                info = client.publish("device/telemetry", json.dumps(payload, separators=(",", ":")), qos=1)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError(f"MQTT publish failed with code {info.rc}")
                pending.append(info)
            for info in pending:
                info.wait_for_publish(timeout=5)
                if not info.is_published():
                    raise TimeoutError("MQTT QoS 1 acknowledgement exceeded five seconds")
            published += len(pending)
            duration = time.monotonic() - cycle_started
            max_cycle_seconds = max(max_cycle_seconds, duration)
            if time.monotonic() > scheduled + 5:
                late_cycles += 1
            if cycle % 12 == 11 or cycle == args.cycles - 1:
                print(json.dumps({"cycle": cycle + 1, "published": published,
                                  "max_cycle_seconds": round(max_cycle_seconds, 3),
                                  "late_cycles": late_cycles}), flush=True)
    finally:
        client.loop_stop()
        client.disconnect()
    print(json.dumps({"run_id": args.run_id, "devices": 200, "interval_seconds": 5,
                      "cycles": args.cycles, "published": published,
                      "elapsed_seconds": round(time.monotonic() - started, 3),
                      "max_cycle_seconds": round(max_cycle_seconds, 3),
                      "late_cycles": late_cycles}), flush=True)


if __name__ == "__main__":
    main()
