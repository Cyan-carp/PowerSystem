"""Exercise the stage-two HTTP, MQTT, TDengine and WebSocket flow on localhost."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt


def request(base: str, method: str, path: str, body: dict | None = None, token: str = "") -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


class WS:
    def __init__(self, host: str, ticket: str):
        self.sock = socket.create_connection((host, 8080), timeout=10)
        self.sock.settimeout(10)
        key = base64.b64encode(os.urandom(16)).decode()
        request_bytes = (
            f"GET /ws/realtime?ticket={urllib.parse.quote(ticket)} HTTP/1.1\r\n"
            f"Host: {host}:8080\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: http://{host}:8080\r\n\r\n"
        ).encode()
        self.sock.sendall(request_bytes)
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(4096)
        header, self.buffer = response.split(b"\r\n\r\n", 1)
        if not header.startswith(b"HTTP/1.1 101"):
            raise RuntimeError(f"WebSocket handshake failed: {header[:100]!r}")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if b"Sec-WebSocket-Accept: " + accept not in header:
            raise RuntimeError("WebSocket accept key mismatch")

    def read_exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def event(self) -> dict:
        first, second = self.read_exact(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.read_exact(8))[0]
        if first & 0x0F != 1:
            raise RuntimeError("unexpected non-text WebSocket frame")
        return json.loads(self.read_exact(length))

    def until(self, event_type: str, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.2, deadline - time.monotonic()))
            event = self.event()
            if event.get("type") == event_type:
                return event
        raise TimeoutError(f"missing {event_type} event")

    def close(self) -> None:
        self.sock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--admin-token-file", type=Path, help="private admin token for CRUD checks; operator remains the query/ack identity")
    args = parser.parse_args()
    admin_token = args.admin_token_file.read_text(encoding="utf-8-sig").strip() if args.admin_token_file else ""
    api_request = globals()["request"]
    def request(base, method, path, body=None, token=""):
        # Stage4 tightened CRUD to admin; retain operator for read and ack tests.
        if admin_token and method in ("POST", "PUT", "DELETE") and (path.startswith("/api/v1/devices") or path.startswith("/api/v1/alarm-rules")):
            token = admin_token
        return api_request(base, method, path, body, token)
    base = args.base.rstrip("/")
    unique = uuid.uuid4().hex[:8]
    username, password = f"smoke_{unique}", f"Smoke-{uuid.uuid4().hex}!a9"
    device_code, run_id = f"INV-SMOKE-{unique}", f"smoke_{unique}"
    cases: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        cases.append((name, condition, detail))
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    client: mqtt.Client | None = None
    ws: WS | None = None
    try:
        status, data = request(base, "GET", "/api/v1/ping")
        check("01 ping", status == 200 and data["code"] == 0)
        status, _ = request(base, "GET", "/api/v1/devices")
        check("02 no token", status == 401)
        status, data = request(base, "POST", "/api/v1/auth/register", {"username": username, "password": password})
        check("03 register", status == 201 and data["data"]["role"] == "operator")
        status, _ = request(base, "POST", "/api/v1/auth/register", {"username": username, "password": password})
        check("04 duplicate account", status == 409)
        status, _ = request(base, "POST", "/api/v1/auth/login", {"username": username, "password": "wrong"})
        check("05 wrong password", status == 401)
        status, data = request(base, "POST", "/api/v1/auth/login", {"username": username, "password": password})
        check("06 login", status == 200 and bool(data["data"]["token"]))
        token = data["data"]["token"]
        status, _ = request(base, "GET", "/api/v1/devices", token="invalid")
        check("07 invalid token", status == 401)
        status, data = request(base, "POST", "/api/v1/devices", {"device_code": device_code, "name": "Smoke", "dev_type": "inverter", "station_code": "ST-01", "group_name": "smoke"}, token)
        check("08 create dynamic device", status == 201)
        device_id = data["data"]["id"]
        status, data = request(base, "GET", "/api/v1/devices?group_name=smoke&page=1&page_size=2", token=token)
        check("09 filter and pagination", status == 200 and any(item["id"] == device_id for item in data["data"]["list"]))
        status, data = request(base, "GET", f"/api/v1/devices/{device_id}", token=token)
        check("10 device detail", status == 200 and data["data"]["device_code"] == device_code)
        status, _ = request(base, "GET", "/api/v1/devices?page=0", token=token)
        check("10a invalid pagination", status == 400)
        status, data = request(base, "PUT", f"/api/v1/devices/{device_id}", {"name": "Smoke updated", "dev_type": "inverter", "group_name": "smoke"}, token)
        check("11 update device", status == 200 and data["data"]["name"] == "Smoke updated")
        status, _ = request(base, "GET", f"/api/v1/devices/{device_id}/telemetry/latest", token=token)
        check("12 latest empty", status == 404)
        status, data = request(base, "POST", "/api/v1/alarm-rules", {"device_id": device_id, "metric": "temperature", "operator": ">", "threshold": 50, "level": "major"}, token)
        check("13 create rule", status == 201)
        rule_id = data["data"]["id"]
        status, _ = request(base, "POST", "/api/v1/alarm-rules", {"device_id": device_id, "metric": "bad", "operator": ">", "threshold": 50, "level": "major"}, token)
        check("14 invalid metric", status == 400)
        status, data = request(base, "GET", f"/api/v1/alarm-rules?device_id={device_id}", token=token)
        check("14a rule list", status == 200 and any(item["id"] == rule_id for item in data["data"]["list"]))
        status, data = request(base, "GET", f"/api/v1/alarm-rules/{rule_id}", token=token)
        check("14b rule detail", status == 200 and data["data"]["metric"] == "temperature")
        status, data = request(base, "PUT", f"/api/v1/alarm-rules/{rule_id}", {"device_id": device_id, "metric": "temperature", "operator": ">", "threshold": 55, "level": "major", "enabled": True}, token)
        check("14c rule update", status == 200 and data["data"]["threshold"] == 55)
        status, _ = request(base, "GET", "/ws/realtime?ticket=bad")
        check("15 invalid WS ticket", status == 401)
        status, data = request(base, "POST", "/api/v1/ws-ticket", token=token)
        check("16 WS ticket", status == 200 and len(data["data"]["ticket"]) == 48)
        ws = WS("127.0.0.1", data["data"]["ticket"])
        check("17 WS connect", ws is not None)
        status, _ = request(base, "GET", f"/ws/realtime?ticket={data['data']['ticket']}")
        check("17a WS ticket single use", status == 401)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"smoke-{unique}")
        client.connect("127.0.0.1", 1883, 30)
        client.loop_start()

        def publish(seq: int, temperature: float) -> dict:
            payload = {"schema_version": 1, "run_id": run_id, "device_id": device_code, "station_id": "ST-01", "seq": seq, "ts_ms": int(time.time() * 1000), "voltage": 400.0, "current": 10.0, "temperature": temperature, "power": 6.86, "status": 1, "fault_code": 0}
            info = client.publish("device/telemetry", json.dumps(payload), qos=1)
            info.wait_for_publish(10)
            if not info.is_published():
                raise RuntimeError("MQTT publish not acknowledged")
            return payload

        alarm_start = time.monotonic()
        publish(0, 70)
        created = ws.until("alarm_created")
        check("18 high temperature alarm push", created["data"]["device_id"] == device_id)
        alarm_latency = time.monotonic() - alarm_start
        check("18a alarm latency within 10s", alarm_latency <= 10, f"{alarm_latency:.3f}s")
        alarm_id = created["data"]["id"]
        status, data = request(base, "GET", f"/api/v1/devices/{device_id}/telemetry/latest", token=token)
        check("19 Redis latest", status == 200 and data["data"]["seq"] == 0)
        status, data = request(base, "POST", f"/api/v1/alarms/{alarm_id}/ack", {}, token)
        check("20 acknowledge", status == 200 and data["data"]["status"] == "acked")
        status, data = request(base, "GET", f"/api/v1/alarms/{alarm_id}", token=token)
        check("20a alarm detail", status == 200 and data["data"]["acked_by"] is not None)
        duplicate_payload = publish(1, 70)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            _, latest_data = request(base, "GET", f"/api/v1/devices/{device_id}/telemetry/latest", token=token)
            if latest_data.get("data", {}).get("seq") == 1:
                break
            time.sleep(0.3)
        check("21a second sample processed", latest_data.get("data", {}).get("seq") == 1)
        repeated = client.publish("device/telemetry", json.dumps(duplicate_payload), qos=1)
        repeated.wait_for_publish(10)
        check("21b duplicate MQTT acknowledged", repeated.is_published())
        time.sleep(2.5)
        status, data = request(base, "GET", "/api/v1/alarms?page=1&page_size=100", token=token)
        count = sum(item["device_id"] == device_id for item in data["data"]["list"])
        check("21 acknowledged overlimit dedup", count == 1)
        publish(2, 30)
        recovered = ws.until("alarm_recovered")
        check("22 recovery push", recovered["data"]["id"] == alarm_id)
        publish(3, 70)
        second = ws.until("alarm_created")
        check("23 re-alarm after recovery", second["data"]["id"] != alarm_id)
        now = datetime.now(timezone.utc)
        query = urllib.parse.urlencode({"metric": "temperature", "start": (now - timedelta(minutes=5)).isoformat(timespec="seconds"), "end": (now + timedelta(minutes=5)).isoformat(timespec="seconds")})
        status, data = request(base, "GET", f"/api/v1/devices/{device_id}/telemetry?{query}", token=token)
        check("24 TDengine history and duplicate dedup", status == 200 and len(data["data"]["points"]) == 4)
        status, _ = request(base, "GET", f"/api/v1/devices/{device_id}/telemetry?metric=invalid&start=2026-01-01T00:00:00Z&end=2026-01-01T01:00:00Z", token=token)
        check("24a invalid history metric", status == 400)
        status, data = request(base, "GET", "/api/v1/dashboard/summary", token=token)
        check("25 dashboard", status == 200 and data["data"]["device_total"] >= 4)
        status, _ = request(base, "DELETE", f"/api/v1/alarm-rules/{rule_id}", token=token)
        check("26 delete rule", status == 200)
        status, _ = request(base, "DELETE", f"/api/v1/devices/{device_id}", token=token)
        check("27 soft delete device", status == 200)
        status, _ = request(base, "GET", f"/api/v1/devices/{device_id}", token=token)
        check("28 deleted device hidden", status == 404)
    finally:
        if ws:
            ws.close()
        if client:
            client.loop_stop()
            client.disconnect()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(("case", "passed", "detail"))
            writer.writerows(cases)
        print(f"{sum(passed for _, passed, _ in cases)}/{len(cases)} smoke cases passed; {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
