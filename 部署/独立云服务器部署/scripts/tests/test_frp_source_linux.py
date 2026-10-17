"""Actual Unix HTTP/control sockets with fake cloud/host adapters, Linux only."""
import http.client
import json
import multiprocessing
import os
import socket
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from frp_source.common import signature
from test_frp_source import TestDirectory

KEY = b"linux-isolated-fixture-key-not-production"


def executor_worker(directory, path, uid):
    from frp_source import service
    fake = Mock()
    fake.check.return_value = {"ready": True}
    fake.refresh.return_value = {"allowlist_updated": True, "changed": False, "recovered": False}
    with patch.object(service, "STATE", Path(directory)), patch.object(service, "CONTROL", path), \
         patch.object(service, "controller", return_value=({"client_id": "fixture"}, fake)), \
         patch.object(service, "load_key", return_value=KEY), \
         patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=uid)):
        service.run_executor()


def receiver_worker(web, control, uid):
    from frp_source import service
    with patch.object(service, "WEB", web), patch.object(service, "CONTROL", control), \
         patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=uid)):
        service.run_receiver()


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=4)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux peer credentials required")
class LinuxSockets(unittest.TestCase):
    def setUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        # Unix socket pathname has a 108-byte limit. Linux runner uses a short root.
        self.web = str(Path(self.directory.name) / "w")
        self.control = str(Path(self.directory.name) / "c")
        self.processes = []
        self.addCleanup(self.stop)
        self.spawn(executor_worker, self.directory.name, self.control, os.getuid())
        self.spawn(receiver_worker, self.web, self.control, os.getuid())

    def spawn(self, target, *args):
        path = self.control if target is executor_worker else self.web
        if Path(path).exists():
            self.assertEqual(Path(path).parent.resolve(), Path(self.directory.name).resolve())
            self.assertTrue(Path(path).is_socket())
            Path(path).unlink()
        process = multiprocessing.get_context("fork").Process(target=target, args=args)
        process.start()
        self.processes.append(process)
        deadline = time.monotonic() + 4
        while not Path(path).exists() and time.monotonic() < deadline:
            self.assertTrue(process.is_alive())
            time.sleep(0.01)
        self.assertTrue(Path(path).exists())

    def stop(self):
        for process in reversed(self.processes):
            process.terminate()
            process.join(timeout=3)
            self.assertFalse(process.is_alive())

    def post(self, operation, body, source="1.1.1.1"):
        connection = UnixHTTP(self.web)
        headers = {"Content-Type": "application/json"}
        if source:
            headers["X-Powersystem-Source"] = source
        try:
            connection.request("POST", "/_ops/frp-source/v1/" + operation, json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_end_to_end_challenge_refresh_and_replay(self):
        status, challenge = self.post("challenge", {"client_id": "fixture"})
        self.assertEqual(status, 200)
        body = {k: challenge[k] for k in ("client_id", "challenge_id", "nonce")}
        body["signature"] = signature(KEY, challenge)
        status, result = self.post("refresh", body)
        self.assertEqual(status, 200)
        self.assertTrue(result["allowlist_updated"])
        self.assertEqual(self.post("refresh", body)[0], 403)

    def test_peer_source_header_is_required(self):
        self.assertEqual(self.post("challenge", {"client_id": "fixture"}, source=None)[0], 503)

    def test_unapproved_operation_is_refused(self):
        self.assertEqual(self.post("execute", {"command": "anything"})[0], 503)

    def test_wrong_signature_cannot_call_fake_cloud(self):
        _, challenge = self.post("challenge", {"client_id": "fixture"})
        body = {k: challenge[k] for k in ("client_id", "challenge_id", "nonce")}
        self.assertEqual(self.post("refresh", body | {"signature": "0" * 64})[0], 403)

    def test_executor_refuses_wrong_peer_uid(self):
        for process in reversed(self.processes):
            process.terminate()
            process.join(timeout=3)
        self.processes.clear()
        # Stale sockets belong to these test processes and are safely replaced.
        self.spawn(executor_worker, self.directory.name, self.control, os.getuid() + 1)
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(3)
            connection.connect(self.control)
            connection.sendall(b'{"operation":"challenge","source":"1.1.1.1","body":{"client_id":"fixture"}}\n')
            with connection.makefile("rb") as stream:
                self.assertEqual(json.loads(stream.readline()), {"ok": False})


if __name__ == "__main__":
    unittest.main()
