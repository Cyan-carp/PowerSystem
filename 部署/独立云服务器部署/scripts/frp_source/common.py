import hashlib
import hmac
import ipaddress
import json
import os
import re
import ssl
import stat
import urllib.request
from pathlib import Path
from uuid import uuid4

PREFIX = "/_ops/frp-source/v1/"
CLOUD_PIN = "SHA256:PUS6mFBOfwdsITlg6Wxl0PP5FYg9wo/4pqrxADeJsyQ"


class GuardError(Exception):
    """Only fixed, non-sensitive codes are allowed across the public boundary."""


def require(condition, code):
    if not condition:
        raise GuardError(code)


def public_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise GuardError("public_ipv4_required") from None
    require(address.is_global and not address.is_multicast, "public_ipv4_required")
    return str(address)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def private_read(path, *, owner=0):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == owner
            and not info.st_mode & 0o077, "unsafe_private_file")
    return path.read_text(encoding="utf-8").strip()


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, sort_keys=True, indent=2) + "\n")


def atomic_text(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def load_key(path):
    value = private_read(path)
    require(bool(re.fullmatch(r"[0-9a-f]{64}", value)), "invalid_device_key")
    return bytes.fromhex(value)


def signature(key, challenge):
    fields = ["frp-source/v1", "POST", PREFIX + "refresh", challenge["client_id"],
              challenge["challenge_id"], challenge["nonce"], challenge["source"]]
    return hmac.new(key, "\n".join(fields).encode(), hashlib.sha256).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GuardError("redirect_refused")


def direct_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                      urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def bounded_json(response, limit=2_000_000):
    data = response.read(limit + 1)
    require(len(data) <= limit, "response_too_large")
    value = json.loads(data)
    require(isinstance(value, dict), "invalid_response")
    return value
