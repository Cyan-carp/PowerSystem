#!/usr/bin/env python3
"""Fixed-scope source recovery CLI. See scripts/README.md before installation."""
import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

from frp_source.common import GuardError, atomic_json, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["initialize", "check", "rollback-pending", "serve-executor", "serve-web", "client", "make-key"])
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "make-key":
        require(os.geteuid() == 0, "root_required")
        path = Path("/etc/powersystem-edge/source-refresh.key")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            stream.write(secrets.token_hex(32) + "\n")
        return {"key_created": True}
    if args.action == "client":
        from frp_source.client import STATE, refresh
        try:
            return refresh()
        except Exception:
            atomic_json(STATE / "status.json", {"allowlist_updated": False, "entry_recovered": False, "checked_at": time.time()})
            raise
    from frp_source.service import STATE, controller, exclusive, run_executor, run_receiver
    if args.action == "serve-web":
        run_receiver()
        return {}
    if args.action == "serve-executor":
        run_executor()
        return {}
    _, reconciler = controller()
    with exclusive(STATE / "refresh.lock"):
        return {"initialize": reconciler.initialize, "check": reconciler.check,
                "rollback-pending": reconciler.rollback}[args.action]()


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except Exception as error:
        code = str(error) if isinstance(error, GuardError) else "operation_failed"
        print(json.dumps({"ok": False, "code": code}), file=sys.stderr)
        sys.exit(1)
