"""Publish a normal synthetic 30-minute window after checking model says low."""

import argparse
import json
import time
import urllib.request

import paho.mqtt.client as mqtt

from replay import sample


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--device-id", required=True, type=int)
    parser.add_argument("--broker", default="emqx")
    args = parser.parse_args()
    end_ms = int(time.time() // 60) * 60_000 + 1000
    run_id = "m4ai-low-" + str(end_ms)
    points = [sample(args.device, "ST-01", run_id, seq,
                     end_ms - (29 - seq) * 60_000,
                     end_ms + 120 * 60_000) for seq in range(30)]
    body = json.dumps({"device_id": args.device_id, "window_end_ms": end_ms,
                       "samples": points}).encode()
    request = urllib.request.Request("http://ai-predict:8090/predict", body,
                                     {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        prediction = json.load(response)
    probability = float(prediction["probability"])
    threshold = float(prediction["threshold"])
    print(json.dumps({"model_probability": probability, "threshold": threshold,
                      "risk_level": prediction["risk_level"]}), flush=True)
    if probability >= threshold or prediction["risk_level"] != "low":
        raise RuntimeError("normal synthetic window did not test as low risk")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="m4-ai-low-" + str(end_ms), clean_session=True)
    client.connect(args.broker, 1883, keepalive=30)
    client.loop_start()
    try:
        for point in points:
            info = client.publish("device/telemetry", json.dumps(point), qos=1)
            info.wait_for_publish(timeout=5)
            if not info.is_published():
                raise TimeoutError("MQTT acknowledgement missing")
            time.sleep(0.05)
    finally:
        client.loop_stop()
        client.disconnect()
    print(json.dumps({"published_low_points": len(points), "window_end_ms": end_ms}), flush=True)


if __name__ == "__main__":
    main()
