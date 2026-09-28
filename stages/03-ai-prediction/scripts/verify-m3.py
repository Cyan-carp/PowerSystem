"""Run and record the Stage 3 PostgreSQL, alarm and WebSocket acceptance path.

Run from the repository root after the M2 stack and AI service are started.
The full check takes roughly 43 minutes because fault and recovery timestamps
must stay fresh for the existing ingestion and prediction workers.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / "stages" / "03-ai-prediction"
STAGE2_SMOKE = ROOT / "stages" / "02-backend" / "scripts" / "smoke-stage2.py"
DOCKER = shutil.which("docker") or str(Path(os.environ.get("LOCALAPPDATA", "")) /
                                  "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe")
COMPOSE = [DOCKER, "compose", "--project-name", "powersystem", "--env-file", str(ROOT / ".env"),
           "-f", str(ROOT / "stages" / "01-data-chain" / "compose.yaml"),
           "-f", str(ROOT / "stages" / "02-backend" / "compose.yaml")]
PORTS = {"emqx": 1883, "tdengine": 6041, "postgres": 5432, "redis": 6379, "go_api": 8080, "fastapi": 8090}


def load_stage2_helpers():
    """Reuse the M2-tested HTTP and raw WebSocket client in this test only."""
    spec = importlib.util.spec_from_file_location("stage2_smoke", STAGE2_SMOKE)
    if spec is None or spec.loader is None:
        raise RuntimeError("stage-two smoke client unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.request, module.WS


def check_environment() -> dict[str, object]:
    ports = {}
    for name, port in PORTS.items():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                ports[name] = True
        except OSError:
            ports[name] = False
    try:
        docker = DOCKER if Path(DOCKER).is_file() else None
    except OSError:
        docker = None
    try:
        paho_available = importlib.util.find_spec("paho.mqtt.client") is not None
    except ModuleNotFoundError:
        paho_available = False
    report: dict[str, object] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "docker_cli": bool(docker),
        "model_present": (ROOT / "artifacts" / "stage3" / "model" / "model.json").is_file(),
        "env_present": (ROOT / ".env").is_file(),
        "paho_mqtt_available": paho_available,
        "ports": ports,
        "ready": False,
    }
    if docker:
        command = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=15, check=False)
        report["docker_engine"] = command.returncode == 0
        if report["docker_engine"] and report["env_present"]:
            listing = subprocess.run(COMPOSE + ["ps", "--format", "json"], cwd=ROOT,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace",
                                     timeout=20, check=False)
            services = {}
            if listing.returncode == 0:
                for line in listing.stdout.splitlines():
                    if line.strip():
                        item = json.loads(line)
                        services[item["Service"]] = {"state": item["State"], "health": item.get("Health", "")}
            report["containers"] = services
        else:
            report["containers"] = {}
    else:
        report["docker_engine"] = False
        report["containers"] = {}
    for name, url in (("go_ping", "http://127.0.0.1:8080/api/v1/ping"),
                      ("ai_health", "http://127.0.0.1:8090/health")):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                payload = json.load(response)
                report[name] = (response.status == 200 and payload.get("data", {}).get("status") == "ok"
                                if name == "go_ping" else response.status == 200
                                and payload.get("status") == "ok" and bool(payload.get("model_version")))
        except (OSError, ValueError):
            report[name] = False
    containers = report["containers"]
    healthy = all(name in containers and containers[name]["state"] == "running"
                  and containers[name]["health"] in ("", "healthy")
                  for name in ("emqx", "tdengine", "postgres", "redis"))
    report["ready"] = bool(report["docker_engine"] and healthy and report["model_present"]
                           and report["paho_mqtt_available"]
                           and report["env_present"] and all(ports.values())
                           and report["go_ping"] and report["ai_health"])
    return report


def api(request, method: str, path: str, token: str = "", body: dict | None = None) -> dict:
    status, reply = request("http://127.0.0.1:8080", method, path, body, token)
    if status < 200 or status >= 300 or reply.get("code") != 0:
        raise RuntimeError(f"{method} {path}: HTTP {status}, code={reply.get('code')}")
    return reply["data"]


def pg_scalar(sql: str) -> str:
    container = subprocess.run(COMPOSE + ["ps", "-q", "postgres"], cwd=ROOT,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=20, check=True).stdout.strip()
    if not container:
        raise RuntimeError("PostgreSQL Compose container is not running")
    result = subprocess.run([DOCKER, "exec", container, "psql", "-U", "powersystem", "-d", "powersystem",
                             "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=20, check=True)
    return result.stdout.strip()


def record_ws(ws, events: list[dict], output: Path, needed: set[str], device_id: int, timeout: float) -> dict[str, dict]:
    found: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    with output.open("a", encoding="utf-8") as stream:
        while time.monotonic() < deadline and found.keys() < needed:
            ws.sock.settimeout(min(2.0, max(0.2, deadline - time.monotonic())))
            try:
                event = ws.event()
            except socket.timeout:
                continue
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            events.append(event)
            if event.get("type") in needed and event.get("data", {}).get("device_id") == device_id:
                found[event["type"]] = event
    return found


def wait_prediction(request, token: str, device_id: int, level: str, earliest: int, deadline: float) -> dict:
    while time.monotonic() < deadline:
        status, reply = request("http://127.0.0.1:8080", "GET",
                                f"/api/v1/devices/{device_id}/prediction", None, token)
        if status == 200 and reply.get("code") == 0:
            data = reply["data"]
            if data["risk_level"] == level and int(data["window_end_ms"]) >= earliest and not data["stale"]:
                return data
        elif status not in (404,):
            raise RuntimeError(f"prediction query failed: HTTP {status}, code={reply.get('code')}")
        time.sleep(1)
    raise TimeoutError(f"no fresh {level} prediction for device {device_id}")


def timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def count_predictions(device_id: int, window_end_ms: int, model_version: str) -> int:
    if not re.fullmatch(r"stage3-xgb-[0-9a-f]{12}", model_version):
        raise ValueError("unexpected model version")
    sql = ("SELECT count(*) FROM prediction_records WHERE device_id=" + str(device_id)
           + " AND window_end_ms=" + str(window_end_ms) + " AND model_version='" + model_version + "'")
    return int(pg_scalar(sql))


def restart_stage2() -> None:
    env = os.environ.copy()
    env.update({"AI_ENABLED": "true", "AI_URL": "http://127.0.0.1:8090", "AI_POLL_SECONDS": "10"})
    for script, arguments in (("stop-stage2.ps1", []), ("start-stage2.ps1", ["-SkipPreflight"])):
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "stages" / "02-backend" / "scripts" / script), *arguments],
                       cwd=ROOT, env=env, check=True, timeout=90)


def run_restart_check(output: Path) -> int:
    """Exercise restart deduplication with a fresh window before its two-minute expiry."""
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"status": "failed", "started_at": datetime.now(timezone.utc).isoformat()}
    replay = None
    try:
        environment = check_environment()
        (output / "environment.json").write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")
        if not environment["ready"]:
            raise RuntimeError("M3 services are not ready")
        request, _ = load_stage2_helpers()
        unique = uuid.uuid4().hex[:12]
        username, password = "m3_" + unique, "M3-" + uuid.uuid4().hex + "!"
        api(request, "POST", "/api/v1/auth/register", body={"username": username, "password": password})
        token = api(request, "POST", "/api/v1/auth/login",
                    body={"username": username, "password": password})["token"]
        device_code = "INV-M3-" + unique.upper()
        device = api(request, "POST", "/api/v1/devices", token,
                     {"device_code": device_code, "name": "M3 restart dedup inverter", "dev_type": "inverter",
                      "station_code": "ST-01", "group_name": "m3"})
        device_id = int(device["id"])
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "artifacts" / "stage3" / "pydeps"),
                                                        str(STAGE), env.get("PYTHONPATH", "")]))
        replay = subprocess.run([sys.executable, "-u", "-m", "prediction.replay", "--device", device_code,
                                 "--lead-minutes", "3"], cwd=STAGE, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=30, check=True)
        run_info = json.loads(replay.stdout.splitlines()[0])
        report.update({"device_id": device_id, "device_code": device_code,
                       "run_id": run_info["run_id"], "window_end_ms": run_info["window_end_ms"],
                       "fault_onset_ms": run_info["fault_onset_ms"]})
        if run_info["published_history"] != 30:
            raise RuntimeError("replay did not publish 30 history points")
        high = wait_prediction(request, token, device_id, "high", int(run_info["window_end_ms"]),
                               time.monotonic() + 45)
        model = high["model_version"]
        before_predictions = count_predictions(device_id, int(high["window_end_ms"]), model)
        before_alarms = int(pg_scalar("SELECT count(*) FROM alarm_records WHERE device_id=" + str(device_id)
                                      + " AND metric='ai_failure_risk'"))
        if before_predictions != 1 or before_alarms != 1:
            raise RuntimeError("fresh high-risk window did not create exactly one prediction and alarm")
        report["before_restart"] = {"prediction_count": before_predictions, "alarm_count": before_alarms,
                                    "risk_level": high["risk_level"], "probability": high["probability"]}
        print(json.dumps({"stage": "before_restart", "device_id": device_id,
                          "window_end_ms": high["window_end_ms"]}, ensure_ascii=False), flush=True)
        restart_stage2()
        time.sleep(20)
        after_predictions = count_predictions(device_id, int(high["window_end_ms"]), model)
        after_alarms = int(pg_scalar("SELECT count(*) FROM alarm_records WHERE device_id=" + str(device_id)
                                     + " AND metric='ai_failure_risk'"))
        latest = api(request, "GET", f"/api/v1/devices/{device_id}/prediction", token)
        report["after_restart"] = {"prediction_count": after_predictions, "alarm_count": after_alarms,
                                   "api_risk_level": latest["risk_level"], "api_stale": latest["stale"]}
        if after_predictions != 1 or after_alarms != 1 or latest["stale"]:
            raise RuntimeError("restart duplicated or lost the fresh prediction/alarm")
        if int(latest["window_end_ms"]) != int(high["window_end_ms"]):
            raise RuntimeError("prediction window changed during restart check")
        report["status"] = "passed"
        print(json.dumps({"status": "passed", "run_id": report["run_id"],
                          "before_restart": report["before_restart"],
                          "after_restart": report["after_restart"]}, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        report["error"] = str(error)
        print(f"M3 restart check failed: {error}", file=sys.stderr)
        return 1
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / "restart-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def run(output: Path, preflight_only: bool, skip_restart: bool) -> int:
    output.mkdir(parents=True, exist_ok=True)
    environment = check_environment()
    (output / "environment.json").write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")
    if preflight_only or not environment["ready"]:
        print(json.dumps(environment, ensure_ascii=False))
        return 0 if environment["ready"] else 2

    request, WS = load_stage2_helpers()
    cases: dict[str, object] = {}
    events: list[dict] = []
    replay: subprocess.Popen | None = None
    ws = None
    log = (output / "replay.stderr.log").open("w", encoding="utf-8")
    summary: dict[str, object] = {"status": "failed", "cases": cases, "run_id": None}
    try:
        unique = uuid.uuid4().hex[:12]
        username, password = "m3_" + unique, "M3-" + uuid.uuid4().hex + "!"
        api(request, "POST", "/api/v1/auth/register", body={"username": username, "password": password})
        token = api(request, "POST", "/api/v1/auth/login",
                    body={"username": username, "password": password})["token"]
        device_code = "INV-M3-" + unique.upper()
        device = api(request, "POST", "/api/v1/devices", token,
                     {"device_code": device_code, "name": "M3 isolated inverter", "dev_type": "inverter",
                      "station_code": "ST-01", "group_name": "m3"})
        device_id = int(device["id"])
        cases["isolated_device_created"] = device_code
        ticket = api(request, "POST", "/api/v1/ws-ticket", token)["ticket"]
        ws = WS("127.0.0.1", ticket)
        cases["websocket_connected"] = True

        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "artifacts" / "stage3" / "pydeps"),
                                                        str(STAGE), env.get("PYTHONPATH", "")]))
        replay = subprocess.Popen([sys.executable, "-u", "-m", "prediction.replay", "--device", device_code,
                                   "--lead-minutes", "3", "--full-cycle"], cwd=STAGE, env=env, stdout=subprocess.PIPE, stderr=log,
                                  text=True)
        assert replay.stdout is not None
        first_line = replay.stdout.readline()
        if not first_line:
            raise RuntimeError(f"replay exited before publishing history: {replay.poll()}")
        run_info = json.loads(first_line)
        summary.update({"run_id": run_info["run_id"], "device_id": device_id, "device_code": device_code,
                        "window_end_ms": run_info["window_end_ms"],
                        "fault_onset_ms": run_info["fault_onset_ms"]})
        if run_info["published_history"] != 30:
            raise RuntimeError("replay did not publish 30 history points")
        onset = int(run_info["fault_onset_ms"])
        remaining = onset / 1000 - time.time() - 5
        if remaining <= 0:
            raise RuntimeError("fault lead time expired before checking prediction")
        high = wait_prediction(request, token, device_id, "high", int(run_info["window_end_ms"]),
                               time.monotonic() + remaining)
        if int(high["window_end_ms"]) >= onset or float(high["probability"]) < float(high["threshold"]):
            raise RuntimeError("high prediction was not made before fault onset")
        cases["pre_fault_high_prediction"] = high
        expected = {"prediction", "alarm_created"}
        pushed = record_ws(ws, events, output / "websocket.jsonl", expected, device_id,
                           max(1.0, onset / 1000 - time.time() - 2))
        if pushed.keys() < expected:
            raise RuntimeError("prediction or alarm WebSocket event missing before fault")
        alarm = pushed["alarm_created"]["data"]
        if alarm["metric"] != "ai_failure_risk" or timestamp_ms(alarm["triggered_at"]) >= onset:
            raise RuntimeError("AI alarm timestamp/metric is invalid")
        alarm_id = int(alarm["id"])
        cases["pre_fault_alarm_websocket"] = alarm_id
        if count_predictions(device_id, int(high["window_end_ms"]), high["model_version"]) != 1:
            raise RuntimeError("high prediction was not stored exactly once in PostgreSQL")
        cases["postgres_high_prediction"] = True
        print(json.dumps({"stage": "pre_fault", "run_id": summary["run_id"],
                          "device_id": device_id, "window_end_ms": high["window_end_ms"],
                          "fault_onset_ms": onset, "probability": high["probability"],
                          "alarm_id": alarm_id}, ensure_ascii=False), flush=True)
        alarm_read = api(request, "GET", f"/api/v1/alarms/{alarm_id}", token)
        if alarm_read["metric"] != "ai_failure_risk" or alarm_read["recovered_at"] is not None:
            raise RuntimeError("AI alarm unavailable from authenticated API")
        acked = api(request, "POST", f"/api/v1/alarms/{alarm_id}/ack", token, {})
        if acked["status"] != "acked":
            raise RuntimeError("AI alarm acknowledgement failed")
        cases["alarm_acknowledged"] = True

        replay.wait(timeout=47 * 60)
        if replay.returncode != 0:
            raise RuntimeError(f"replay exited with {replay.returncode}")
        cases["fault_and_recovery_published"] = True
        low = wait_prediction(request, token, device_id, "low", onset + 39 * 60_000,
                              time.monotonic() + 45)
        cases["recovery_low_prediction"] = low
        recovered = record_ws(ws, events, output / "websocket.jsonl", {"alarm_recovered"}, device_id, 15)
        if "alarm_recovered" not in recovered or int(recovered["alarm_recovered"]["data"]["id"]) != alarm_id:
            raise RuntimeError("AI alarm recovery WebSocket event missing")
        cases["alarm_recovery_websocket"] = True
        alarm_read = api(request, "GET", f"/api/v1/alarms/{alarm_id}", token)
        if alarm_read["status"] != "recovered" or alarm_read["recovered_at"] is None:
            raise RuntimeError("AI alarm was not recovered in PostgreSQL")
        if count_predictions(device_id, int(low["window_end_ms"]), low["model_version"]) != 1:
            raise RuntimeError("low prediction was not stored exactly once in PostgreSQL")
        cases["postgres_recovery_and_dedup"] = True
        print(json.dumps({"stage": "recovered", "run_id": summary["run_id"],
                          "window_end_ms": low["window_end_ms"], "probability": low["probability"],
                          "alarm_id": alarm_id}, ensure_ascii=False), flush=True)

        if not skip_restart:
            ws.close()
            ws = None
            restart_stage2()
            time.sleep(20)
            if count_predictions(device_id, int(low["window_end_ms"]), low["model_version"]) != 1:
                raise RuntimeError("restart created duplicate prediction")
            if count_predictions(device_id, int(high["window_end_ms"]), high["model_version"]) != 1:
                raise RuntimeError("restart changed the earlier high prediction")
            if api(request, "GET", f"/api/v1/alarms/{alarm_id}", token)["status"] != "recovered":
                raise RuntimeError("restart changed alarm state")
            if int(pg_scalar("SELECT count(*) FROM alarm_records WHERE device_id=" + str(device_id)
                             + " AND metric='ai_failure_risk'")) != 1:
                raise RuntimeError("restart created another AI alarm")
            cases["restart_idempotence"] = True
        summary["status"] = "passed" if not skip_restart else "partial_without_restart"
        print(json.dumps({"status": summary["status"], "run_id": summary["run_id"],
                          "cases": list(cases)}, ensure_ascii=False))
        return 0 if not skip_restart else 3
    except Exception as error:
        summary["error"] = str(error)
        print(f"M3 verification failed: {error}", file=sys.stderr)
        return 1
    finally:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["websocket_event_count"] = len(events)
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if ws is not None:
            ws.close()
        if replay is not None and replay.poll() is None:
            replay.terminate()
            try:
                replay.wait(timeout=10)
            except subprocess.TimeoutExpired:
                replay.kill()
        log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--restart-check", action="store_true", help="fresh-window restart dedup check")
    parser.add_argument("--skip-restart", action="store_true", help="diagnostic only; does not pass full M3")
    args = parser.parse_args()
    if args.restart_check:
        return run_restart_check(args.output)
    return run(args.output, args.preflight_only, args.skip_restart)


if __name__ == "__main__":
    raise SystemExit(main())
