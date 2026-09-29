"""Measure authenticated public device-list and one-hour history latency."""

import argparse
import json
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


def request(base, path, token=None, payload=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers)
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=10) as response:
        body = json.load(response)
        status = response.status
    elapsed = time.perf_counter() - start
    if status != 200 or body.get("code") != 0:
        raise RuntimeError(f"{path}: HTTP {status}, business code {body.get('code')}")
    return body["data"], elapsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="https://8.138.10.222")
    parser.add_argument("--user", default="p0-admin")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("samples must be 1..100")
    password = args.password_file.read_text(encoding="utf-8").strip()
    login, _ = request(args.base, "/api/v1/auth/login",
                       payload={"username": args.user, "password": password})
    token = login["token"]
    devices, _ = request(args.base, "/api/v1/devices?page=1&page_size=20", token)
    if not devices["list"]:
        raise RuntimeError("no device available for history timing")
    device_id = devices["list"][0]["id"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)
    times = {"device_list": [], "one_hour_history": []}
    paths = {
        "device_list": "/api/v1/devices?page=1&page_size=20",
        "one_hour_history": "/api/v1/devices/" + str(device_id) + "/telemetry?" +
            urllib.parse.urlencode({"metric": "temperature",
                                    "start": start.isoformat(timespec="seconds"),
                                    "end": end.isoformat(timespec="seconds")}),
    }
    for _ in range(args.samples):
        for name, path in paths.items():
            _, duration = request(args.base, path, token)
            times[name].append(duration)
    result = {name: {"samples": len(values), "median_ms": round(statistics.median(values)*1000, 1),
                     "max_ms": round(max(values)*1000, 1),
                     "all_under_2s": all(value <= 2 for value in values)}
              for name, values in times.items()}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
