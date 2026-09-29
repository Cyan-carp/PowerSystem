"""Controlled live verification of first-version Feishu business notifications.

Run only on the project server from /opt/powersystem. This creates temporary
temperature rules and sends real group messages. It never prints credentials.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:18080"
COMPOSE = ["docker", "compose", "--env-file", ".env", "-f", "stages/04-frontend/deploy/compose.yaml"]


def dc(*args):
    return subprocess.run([*COMPOSE, *args], check=True, capture_output=True, text=True).stdout.strip()


def sql(statement):
    return dc("exec", "-T", "postgres", "psql", "-U", "powersystem", "-d", "powersystem", "-Atqc", statement)


def api(method, path, token=None, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(BASE + path, data=data, headers=headers, method=method)
    with urlopen(request, timeout=10) as response:
        result = json.load(response)
    if result.get("code") != 0:
        raise RuntimeError(f"API {method} {path} returned business code {result.get('code')}")
    return result["data"]


def login(username, password_file):
    password = Path(password_file).read_text(encoding="utf-8").strip()
    return api("POST", "/api/v1/auth/login", payload={"username": username, "password": password})["token"]


def wait_for(label, check, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            print(f"PASS {label}: {result}", flush=True)
            return result
        time.sleep(2)
    raise TimeoutError(label)


def alarm_for_rule(rule_id):
    alarms = api("GET", "/api/v1/alarms?page_size=100", admin_token)["list"]
    for alarm in alarms:
        if alarm["rule_id"] == rule_id and alarm["recovered_at"] is None:
            return alarm
    return None


def outbox(alarm_id, state):
    event_id = f"alarm:{alarm_id}:{state}"
    row = sql("SELECT attempts,CASE WHEN sent_at IS NULL THEN 0 ELSE 1 END "
              f"FROM business_notification_outbox WHERE event_key='{event_id}'")
    if not row:
        return None
    attempts, sent = map(int, row.split("|"))
    return {"event_id": event_id, "attempts": attempts, "sent": bool(sent)}


def create_rule(device_id):
    return api("POST", "/api/v1/alarm-rules", admin_token,
               {"device_id": device_id, "metric": "temperature", "operator": ">",
                "threshold": 0, "level": "minor", "enabled": True})["id"]


def update_rule(rule_id, device_id, threshold, enabled=True):
    return api("PUT", f"/api/v1/alarm-rules/{rule_id}", admin_token,
               {"device_id": device_id, "metric": "temperature", "operator": ">",
                "threshold": threshold, "level": "minor", "enabled": enabled})


def verify_ai_template():
    # This probes the adapter's AI message format and real Feishu acceptance.
    # It does not claim that the model generated a live risk event.
    code = """import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
event = {"event_id": "alarm:900001:triggered", "category": "prediction_risk",
         "state": "triggered", "alarm_id": 900001, "device_code": "TEST-AI-01",
         "station_code": "TEST", "metric": "ai_failure_risk", "level": "major",
         "value": 0.91, "threshold": 0.8,
         "occurred_at": datetime.now(timezone.utc).isoformat()}
token = Path('/run/secrets/feishu_business_token').read_text().strip()
request = Request('http://127.0.0.1:8091/business', data=json.dumps(event).encode(),
                  headers={'Content-Type': 'application/json', 'X-PowerSystem-Token': token},
                  method='POST')
with urlopen(request, timeout=12) as response:
    print(f'ai_template_http={response.status}')
"""
    print(dc("exec", "-T", "feishu-adapter", "python", "-c", code), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="send real test messages to the configured Feishu group")
    parser.add_argument("--ai-template-only", action="store_true", help="send one clearly synthetic AI risk template probe")
    args = parser.parse_args()
    if not args.execute:
        parser.error("pass --execute to acknowledge real group messages")

    if args.ai_template_only:
        verify_ai_template()
        raise SystemExit(0)

    admin_token = login("p0-admin", "/etc/powersystem/p0-admin-password")
    operator_token = login("demo-operator", "/etc/powersystem/demo-password")
    existing = sql("SELECT count(*) FROM alarm_rules WHERE enabled AND metric='temperature' AND device_id IN (1,2)")
    if existing != "0":
        raise RuntimeError("devices 1 or 2 already have an active temperature rule")

    created_rules = []
    adapter_stopped = False
    try:
        dc("stop", "feishu-adapter")
        adapter_stopped = True
        rule_id = create_rule(1)
        created_rules.append(rule_id)
        alarm = wait_for("device alarm triggered", lambda: alarm_for_rule(rule_id))
        alarm_id = alarm["id"]
        wait_for("delivery retry retained while adapter stopped",
                 lambda: (row if (row := outbox(alarm_id, "triggered")) and row["attempts"] >= 1 and not row["sent"] else None))
        dc("start", "feishu-adapter")
        adapter_stopped = False
        wait_for("trigger delivered after adapter recovery",
                 lambda: (row if (row := outbox(alarm_id, "triggered")) and row["sent"] else None))

        api("POST", f"/api/v1/alarms/{alarm_id}/ack", operator_token, {})
        wait_for("operator acknowledgement delivered",
                 lambda: (row if (row := outbox(alarm_id, "acknowledged")) and row["sent"] else None))
        update_rule(rule_id, 1, 100)
        wait_for("natural recovery delivered",
                 lambda: (row if (row := outbox(alarm_id, "recovered")) and row["sent"] else None))

        rule2 = create_rule(2)
        created_rules.append(rule2)
        alarm2 = wait_for("second device alarm triggered", lambda: alarm_for_rule(rule2))
        wait_for("second trigger delivered",
                 lambda: (row if (row := outbox(alarm2["id"], "triggered")) and row["sent"] else None))
        update_rule(rule2, 2, 0, enabled=False)
        wait_for("configuration closure delivered",
                 lambda: (row if (row := outbox(alarm2["id"], "closed")) and row["sent"] else None))
    finally:
        if adapter_stopped:
            dc("start", "feishu-adapter")
        for rule_id in created_rules:
            api("DELETE", f"/api/v1/alarm-rules/{rule_id}", admin_token)
