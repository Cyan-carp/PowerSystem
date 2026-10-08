#!/usr/bin/env python3
"""Read-only HTTPS/WSS smoke check using the private R730xd demo credential.

Run on R730xd: python3 check-auth-wss.py /etc/powersystem/demo-password
Only status codes are printed. Passwords, JWTs and single-use tickets stay in memory.
"""

import base64
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


def request(host, path, method="GET", body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        f"https://{host}{path}", data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        return response.status, json.load(response)


def web_socket(host, ticket):
    path = "/ws/realtime?ticket=" + urllib.parse.quote(ticket, safe="")
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    headers = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Origin: https://{host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    context = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=12) as raw:
        with context.wrap_socket(raw, server_hostname=host) as conn:
            conn.settimeout(12)
            conn.sendall(headers.encode("ascii"))
            line = b""
            while b"\r\n" not in line and len(line) < 4096:
                line += conn.recv(1)
    return line.decode("ascii", errors="replace").strip()


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: check-auth-wss.py /etc/powersystem/demo-password")
    with open(sys.argv[1], encoding="utf-8") as source:
        password = source.read().strip()
    if not password:
        raise RuntimeError("demo credential file is empty")
    for host in ("8.138.42.212", "8.138.10.222"):
        status, login = request(
            host,
            "/api/v1/auth/login",
            method="POST",
            body={"username": "demo-operator", "password": password},
        )
        token = login["data"]["token"]
        print(f"{host} login={status}", flush=True)
        for label, path in (
            ("dashboard", "/api/v1/dashboard/summary"),
            ("devices", "/api/v1/devices?page=1&page_size=20"),
            ("alarms", "/api/v1/alarms?page=1&page_size=20"),
            ("predictions", "/api/v1/predictions?page=1&page_size=20"),
            ("agent-interpretations", "/api/v1/agent/interpretations?page=1&page_size=20"),
        ):
            status, envelope = request(host, path, token=token)
            if status != 200 or envelope.get("code") != 0:
                raise RuntimeError(f"{host} {label} failed")
            print(f"{host} {label}={status}", flush=True)
        status, ticket_response = request(
            host, "/api/v1/ws-ticket", method="POST", token=token
        )
        ticket = ticket_response["data"]["ticket"]
        print(f"{host} ws-ticket={status}", flush=True)
        response_line = web_socket(host, ticket)
        if not response_line.startswith("HTTP/1.1 101 "):
            raise RuntimeError(f"{host} WSS status: {response_line}")
        print(f"{host} WSS=101", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, urllib.error.HTTPError, RuntimeError) as error:
        print(f"FAIL: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
