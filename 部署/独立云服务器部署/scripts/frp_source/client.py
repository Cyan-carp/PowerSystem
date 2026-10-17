import json
import time
import urllib.request
from pathlib import Path

from .common import PREFIX, atomic_json, bounded_json, direct_opener, load_key, private_read, public_ipv4, require, signature
from .service import exclusive

BASE = "https://8.138.42.212"
STATE = Path("/var/lib/powersystem-source-client")


def refresh():
    config = json.loads(private_read("/etc/powersystem-edge/source-refresh-client.json"))
    require(set(config) == {"enabled", "client_id"} and config["enabled"] is True, "client_configuration_not_enabled")
    key = load_key("/etc/powersystem-edge/source-refresh.key")
    opener = direct_opener()

    def post(operation, body):
        request = urllib.request.Request(BASE + PREFIX + operation, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with opener.open(request, timeout=95) as response:
            require(response.status == 200, "refresh_failed")
            return bounded_json(response, 4096)

    with exclusive(STATE / "client.lock"):
        challenge = post("challenge", {"client_id": config["client_id"]})
        require(challenge.get("client_id") == config["client_id"] and challenge.get("expires_in") == 120, "invalid_challenge")
        public_ipv4(challenge.get("source"))
        body = {field: challenge[field] for field in ("client_id", "challenge_id", "nonce")}
        body["signature"] = signature(key, challenge)
        response = post("refresh", body)
        require(response.get("allowlist_updated") is True, "allowlist_update_incomplete")
        entry = False
        try:
            with opener.open(BASE + "/api/v1/ping", timeout=8) as probe:
                entry = probe.status == 200
        except Exception:
            pass
        result = {"allowlist_updated": True, "changed": bool(response.get("changed")), "entry_recovered": entry,
                  "checked_at": time.time()}
        atomic_json(STATE / "status.json", result)
        return result
