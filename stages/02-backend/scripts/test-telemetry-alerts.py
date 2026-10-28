"""TR-S01 real Prometheus/Alertmanager loopback notification acceptance.

Linux/WSL only. No production endpoints, credentials, or third-party notifications.
The metrics source is a deterministic fault fixture; worker/probe I/O is tested in Go.
"""
from __future__ import annotations
import argparse
import json
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BUDGETS = {"InputStopped": 120, "CoverageLow": 105, "WorkerStalled": 150,
           "ProbeFailed": 90, "NoBusinessSuccess": 150, "RedisWriteStalled": 150,
           "recovery": 75, "dedup_window": 45}
TARGETS = {"PowerSystemTelemetry" + k for k in BUDGETS if k not in ("recovery", "dedup_window")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=False)
    rules = Path(__file__).resolve().parents[2] / "04-frontend/deploy/alerts.yml"
    state = {"fault": None, "posts": [], "unexpected": [], "started": time.time() - 60}
    lock = threading.Lock()
    processes = []
    logs = []
    result = {"budgets_before_run": BUDGETS, "scrape": 15, "evaluation": 15,
              "for": 15, "group_wait": 5, "group_interval": 15,
              "repeat_interval": 300, "notifications": [], "passed": False}
    (args.evidence / "budget-before.json").write_text(json.dumps(result, indent=2))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            now = time.time()
            with lock:
                fault = state["fault"]
            age = max(0, now - fault) if fault else 0
            timestamp = fault or now
            values = {"start_timestamp_seconds": state["started"], "probe_pg_ok": 1,
                      "probe_redis_ok": 1, "probe_timestamp_seconds": timestamp,
                      "inbox_receive_age_seconds": age, "business_success_age_seconds": age,
                      "queue_oldest_age_seconds": age, "expected_devices": 2,
                      "fresh_ratio": .5 if age > 15 else 1, "missing_keys": 1 if age > 15 else 0,
                      "cache_received_worst_age_seconds": age, "cache_sample_worst_age_seconds": age,
                      "maintenance": 0, "maintenance_known": 1, "maintenance_age_seconds": 0, "classification_known": 1,
                      "loop_timestamp_seconds": timestamp, "redis_write_timestamp_seconds": timestamp}
            body = "".join(f"powersystem_telemetry_{key} {value}\n" for key, value in values.items()).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                target = state["posts"] if self.path == "/telemetry" else state["unexpected"]
                target.append({"received_at": time.time(), "path": self.path, "payload": payload})
            self.send_response(200); self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 18089), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    prom = args.evidence / "prometheus.yml"
    am = args.evidence / "alertmanager.yml"
    prom.write_text(f"""global:
  scrape_interval: 15s
  evaluation_interval: 15s
rule_files: ['{rules}']
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['127.0.0.1:19093']
scrape_configs:
  - job_name: api
    static_configs:
      - targets: ['127.0.0.1:18089']
""")
    am.write_text("""templates: []
route:
  receiver: unexpected
  group_by: ['component', 'instance']
  group_wait: 5s
  group_interval: 15s
  repeat_interval: 5m
  routes:
    - receiver: tr-s01-local
      matchers: ['component="telemetry"']
receivers:
  - name: unexpected
    webhook_configs:
      - url: http://127.0.0.1:18089/unexpected
        send_resolved: true
  - name: tr-s01-local
    webhook_configs:
      - url: http://127.0.0.1:18089/telemetry
        send_resolved: true
""")
    def start(binary, *arguments):
        output = (args.evidence / (binary + ".log")).open("w")
        logs.append(output)
        processes.append(subprocess.Popen([str(args.bin_dir / binary), *arguments], stdout=output, stderr=output))
    def statuses(status):
        found = {}
        with lock:
            posts = list(state["posts"])
        for post in posts:
            for alert in post["payload"]["alerts"]:
                if alert["status"] == status:
                    found.setdefault(alert["labels"]["alertname"], post["received_at"])
        return found
    def wait_for(predicate, budget):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in processes):
                raise AssertionError("isolated monitoring process exited")
            if predicate(): return
            time.sleep(1)
        raise AssertionError("notification budget exceeded")
    try:
        syntax = subprocess.run([str(args.bin_dir / "promtool"), "check", "rules", str(rules)], capture_output=True, text=True)
        (args.evidence / "promtool.txt").write_text(syntax.stdout + syntax.stderr)
        assert syntax.returncode == 0, "invalid production rules"
        start("prometheus-alertmanager", "--config.file=" + str(am), "--storage.path=" + str(args.evidence / "am-data"), "--web.listen-address=127.0.0.1:19093", "--cluster.listen-address=")
        start("prometheus", "--config.file=" + str(prom), "--storage.tsdb.path=" + str(args.evidence / "prom-data"), "--web.listen-address=127.0.0.1:19090")
        def ready():
            try:
                return urllib.request.urlopen("http://127.0.0.1:19090/-/ready", timeout=1).status == 200
            except Exception: return False
        wait_for(ready, 20)
        time.sleep(20)
        with lock:
            assert not state["posts"] and not state["unexpected"], "healthy baseline alerted"
            state["fault"] = time.time()
            onset = state["fault"]
        wait_for(lambda: TARGETS <= statuses("firing").keys(), 150)
        firing = statuses("firing")
        result["detection_seconds"] = {key: firing[key] - onset for key in sorted(TARGETS)}
        for key, seconds in result["detection_seconds"].items():
            assert seconds <= BUDGETS[key.removeprefix("PowerSystemTelemetry")], (key, seconds)
        # Group membership changes may legitimately re-send existing alerts.
        # Once the full group settles, an unchanged group must stay deduplicated.
        time.sleep(16)
        with lock: count = len(state["posts"])
        time.sleep(BUDGETS["dedup_window"])
        with lock:
            assert len(state["posts"]) == count, "unchanged group was re-notified before repeat interval"
            assert not state["unexpected"], "incorrect receiver route"
            state["fault"] = None
            recovered = time.time()
        wait_for(lambda: TARGETS <= statuses("resolved").keys(), BUDGETS["recovery"])
        resolved = statuses("resolved")
        result["recovery_seconds"] = {key: resolved[key] - recovered for key in sorted(TARGETS)}
        result["dedup_passed"] = True
        result["route"] = "tr-s01-local /telemetry"
        result["passed"] = True
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        with lock: result["notifications"] = state["posts"]; result["unexpected"] = state["unexpected"]
        (args.evidence / "result.json").write_text(json.dumps(result, indent=2))
        for process in reversed(processes):
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        for output in logs: output.close()
        server.shutdown(); server.server_close()
    print(json.dumps({k: v for k, v in result.items() if k != "notifications"}, indent=2))

if __name__ == "__main__": main()
