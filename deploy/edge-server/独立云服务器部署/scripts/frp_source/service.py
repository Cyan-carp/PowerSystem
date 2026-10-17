import contextlib
import json
import os
import re
import socket
import socketserver
import stat
import struct
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .auth import Challenges
from .cloud import Swas
from .common import GuardError, PREFIX, atomic_json, load_key, private_read, public_ipv4, require
from .host import Host
from .reconcile import Reconciler

STATE = Path("/var/lib/powersystem-source")
CONFIG = Path("/etc/powersystem-edge/source-refresh-cloud.json")
CONTROL = "/run/powersystem-source-executor/control.sock"
WEB = "/run/powersystem-source-web/http.sock"
KEY = "/etc/powersystem-edge/source-refresh.key"


@contextlib.contextmanager
def exclusive(path):
    import fcntl
    with open(path, "a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuardError("refresh_busy") from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def cloud_config():
    require(os.geteuid() == 0, "root_required")
    config = json.loads(private_read(CONFIG))
    fields = {"enabled", "provider", "client_id", "region_id", "instance_id", "target_rule_id"}
    require(set(config) == fields and config["enabled"] is True, "cloud_configuration_not_enabled")
    require(config["provider"] == "swas", "sas_provider_required")
    for field in fields - {"enabled"}:
        require(isinstance(config[field], str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", config[field]))
                and "REPLACE" not in config[field], "cloud_configuration_incomplete")
    require(bool(re.fullmatch(r"[a-z]{2}-[a-z0-9-]+", config["region_id"])), "invalid_region")
    for field in ("instance_id", "target_rule_id"):
        require(bool(re.fullmatch(r"[a-f0-9]{32}", config[field])), "invalid_sas_resource")
    info = STATE.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o077, "unsafe_state_directory")
    return config


def controller():
    config = cloud_config()
    cloud = Swas(config)
    cloud.deadline = time.monotonic() + 70
    return config, Reconciler(STATE, Host(), cloud)


def peer_uid(connection):
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]


def serve_socket(path, handler, group):
    path = Path(path)
    # Do not replace an arbitrary file or a still-listening instance.
    if path.exists() or path.is_symlink():
        info = path.lstat()
        require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid(), "unsafe_existing_socket")
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink()
        else:
            raise GuardError("socket_already_active")
        finally:
            probe.close()
    server = socketserver.UnixStreamServer(str(path), handler)
    server.allow_reuse_address = False
    os.chown(path, -1, group)
    path.chmod(0o660)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        path.unlink(missing_ok=True)


def run_executor():
    import pwd
    config, reconciler = controller()
    with exclusive(STATE / "refresh.lock"):
        reconciler.check()
    challenges = Challenges(STATE / "challenges.sqlite3", config["client_id"], load_key(KEY))
    receiver_uid = pwd.getpwnam("powersystem-source").pw_uid

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                require(peer_uid(self.connection) == receiver_uid, "peer_refused")
                self.connection.settimeout(5)
                raw = self.rfile.readline(4097)
                require(len(raw) <= 4096 and raw.endswith(b"\n"), "request_too_large")
                request = json.loads(raw)
                require(set(request) == {"operation", "source", "body"}, "invalid_request")
                source = public_ipv4(request["source"])
                with exclusive(STATE / "refresh.lock"):
                    if request["operation"] == "challenge":
                        result = challenges.issue(source, request["body"])
                    elif request["operation"] == "refresh":
                        source = challenges.consume(source, request["body"])
                        reconciler.cloud.deadline = time.monotonic() + 70
                        result = reconciler.refresh(source)
                        atomic_json(STATE / "last-result.json", {**result, "checked_at": time.time()})
                    else:
                        raise GuardError("operation_refused")
                response = {"ok": True, "result": result}
            except Exception as error:
                # Keep even error codes private; public replies carry a generic status.
                code = str(error) if isinstance(error, GuardError) else "internal_failure"
                atomic_json(STATE / "last-error.json", {"code": code, "checked_at": time.time()})
                print(json.dumps({"event": "request_failed", "code": code}), flush=True)
                response = {"ok": False}
            self.wfile.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")

    serve_socket(CONTROL, Handler, os.getegid())


def run_receiver():
    import pwd
    nginx_uid = pwd.getpwnam("www-data").pw_uid

    class Handler(BaseHTTPRequestHandler):
        server_version = ""
        sys_version = ""

        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *args):
            pass

        def do_POST(self):
            status, result = 503, {"error": "refresh_unavailable"}
            try:
                require(peer_uid(self.connection) == nginx_uid, "peer_refused")
                operation = self.path.removeprefix(PREFIX)
                require(self.path in (PREFIX + "challenge", PREFIX + "refresh"), "operation_refused")
                require(not self.headers.get("Transfer-Encoding"), "encoding_refused")
                require(len(self.headers.get_all("Content-Length", [])) == 1, "invalid_length")
                require(self.headers.get_content_type() == "application/json", "invalid_content_type")
                length = int(self.headers["Content-Length"])
                require(0 < length <= 2048, "request_too_large")
                sources = self.headers.get_all("X-Powersystem-Source", [])
                require(len(sources) == 1, "source_header_required")
                source = public_ipv4(sources[0])
                body = json.loads(self.rfile.read(length))
                require(isinstance(body, dict), "invalid_request")
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(85)
                    connection.connect(CONTROL)
                    request = {"operation": operation, "source": source, "body": body}
                    connection.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
                    with connection.makefile("rb") as stream:
                        response = stream.readline(4097)
                    require(len(response) <= 4096, "response_too_large")
                    response = json.loads(response)
                if response.get("ok") is True:
                    status, result = 200, response["result"]
                else:
                    status, result = 403, {"error": "request_refused"}
            except Exception:
                pass
            payload = json.dumps(result, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    serve_socket(WEB, Handler, os.getegid())
