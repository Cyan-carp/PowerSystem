"""Register one disposable synthetic inverter for a real-model notification check."""

import argparse
import json
import time
import urllib.request
from pathlib import Path


def call(base, path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, json.dumps(payload).encode(), headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        body = json.load(response)
        if response.status not in (200, 201) or body.get("code") != 0:
            raise RuntimeError(f"HTTP {response.status}, code {body.get('code')}")
    return body["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="https://8.138.10.222")
    parser.add_argument("--password-file", type=Path, required=True)
    args = parser.parse_args()
    password = args.password_file.read_text(encoding="utf-8").strip()
    token = call(args.base, "/api/v1/auth/login",
                 {"username": "p0-admin", "password": password})["token"]
    code = "INV-M4-AI-" + time.strftime("%Y%m%d%H%M%S", time.gmtime())
    device = call(args.base, "/api/v1/devices",
                  {"device_code": code, "name": "M4 synthetic AI notification check",
                   "dev_type": "inverter", "station_code": "ST-01", "group_name": "m4-ai"}, token)
    print(json.dumps({"device_id": device["id"], "device_code": code}))


if __name__ == "__main__":
    main()
