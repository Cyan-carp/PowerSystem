"""Soft-delete the exact synthetic M4 AI device after inspecting its code."""

import argparse
import json
import urllib.request
from pathlib import Path


def request(base, path, token=None, payload=None, method=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as response:
        body = json.load(response)
        if response.status != 200 or body.get("code") != 0:
            raise RuntimeError(f"HTTP {response.status}, code {body.get('code')}")
    return body["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="https://8.138.10.222")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--device-id", type=int, required=True)
    parser.add_argument("--device-code", required=True)
    args = parser.parse_args()
    if not args.device_code.startswith("INV-M4-AI-"):
        parser.error("refusing to remove a non-M4 synthetic device")
    password = args.password_file.read_text(encoding="utf-8").strip()
    token = request(args.base, "/api/v1/auth/login", payload={"username": "p0-admin",
                                                            "password": password})["token"]
    path = f"/api/v1/devices/{args.device_id}"
    device = request(args.base, path, token)
    if device["device_code"] != args.device_code:
        raise RuntimeError("device id/code mismatch; refusing cleanup")
    request(args.base, path, token, method="DELETE")
    print(json.dumps({"removed_synthetic_device": args.device_code,
                      "device_id": args.device_id}))


if __name__ == "__main__":
    main()
