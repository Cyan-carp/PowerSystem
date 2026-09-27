"""Reconcile a stage-two simulator ledger with the persistent TDengine database."""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sqlite3
import subprocess
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path


def td_sql(query: str, password: str) -> list[list[object]]:
    credentials = base64.b64encode(f"root:{password}".encode()).decode()
    request = urllib.request.Request(
        "http://127.0.0.1:6041/rest/sql",
        data=query.encode(),
        headers={"Authorization": f"Basic {credentials}", "Content-Type": "text/plain"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if result.get("code") != 0:
        raise RuntimeError(f"TDengine {result.get('code')}: {result.get('desc')}")
    return result.get("data", [])


def pg_scalar(docker_exe: str, query: str) -> int:
    result = subprocess.run(
        [docker_exe, "exec", "powersystem-postgres-1", "psql", "-U", "powersystem", "-d", "powersystem", "-Atc", query],
        capture_output=True, text=True, check=True, timeout=20,
    )
    return int(result.stdout.strip())


def sanitized_log(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    allowed = {"level", "ts", "msg", "count", "device_id", "database", "address"}
    with source.open(encoding="utf-8", errors="replace") as input_log, target.open("w", encoding="utf-8") as output_log:
        for line in input_log:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and "msg" in item:
                output_log.write(json.dumps({key: value for key, value in item.items() if key in allowed}, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--docker", required=True)
    parser.add_argument("--gateway-queue", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8-sig"))
    run_id = run["run_id"]
    if not run_id.replace("_", "").isalnum():
        raise ValueError("unsafe run_id")
    password = os.environ["TDENGINE_ROOT_PASSWORD"]
    started = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(run["ended_at"].replace("Z", "+00:00"))
    elapsed_seconds = (ended - started).total_seconds()
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    event_names = {item["event"] for item in events}
    required_events = {"broker_stopped", "broker_restored", "gateway_restarted", "api_restarted"} if run["fault_injection"] else set()
    with (run_dir / "cases.csv").open(encoding="utf-8", newline="") as case_file:
        cases = list(csv.DictReader(case_file))
    cases_passed = len(cases) >= 20 and all(row["passed"] == "True" for row in cases)
    simulator = sqlite3.connect(run_dir / "simulator.sqlite")
    gateway = sqlite3.connect(f"file:{args.gateway_queue.resolve()}?mode=ro", uri=True)
    generated = list(simulator.execute("SELECT device_id,seq,ts_ms FROM generated ORDER BY device_id,seq"))
    simulator_pending = simulator.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    gateway_pending = gateway.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
    details: dict[str, object] = {}
    passed = (len(generated) == 3 * int(run["samples_per_device"]) and simulator_pending == 0 and gateway_pending == 0
              and elapsed_seconds >= int(run["samples_per_device"]) * float(run["interval_seconds"])
              and required_events.issubset(event_names) and cases_passed)
    for device in ("INV-1001", "INV-1002", "INV-1003"):
        expected = [(seq, ts) for code, seq, ts in generated if code == device]
        if not expected:
            details[device] = {"expected": 0, "actual": 0, "missing": [], "duplicates": []}
            passed = False
            continue
        low, high = min(ts for _, ts in expected), max(ts for _, ts in expected)
        rows = td_sql(
            f"SELECT seq FROM powersystem_stage2.telemetry WHERE device_id='{device}' AND ts >= {low} AND ts <= {high} ORDER BY seq",
            password,
        )
        actual = [int(row[0]) for row in rows]
        expected_seq = [seq for seq, _ in expected]
        missing = sorted(set(expected_seq) - set(actual))
        extra = sorted(set(actual) - set(expected_seq))
        duplicates = sorted(seq for seq, count in Counter(actual).items() if count > 1)
        device_passed = len(actual) == len(expected) and not missing and not extra and not duplicates
        passed = passed and device_passed
        details[device] = {"expected": len(expected), "actual": len(actual), "missing": missing, "extra": extra, "duplicates": duplicates}
    inbox_count = pg_scalar(args.docker, f"SELECT COUNT(*) FROM telemetry_inbox WHERE run_id='{run_id}'")
    inbox_pending = pg_scalar(args.docker, f"SELECT COUNT(*) FROM telemetry_inbox WHERE run_id='{run_id}' AND processed_at IS NULL")
    passed = passed and inbox_count == len(generated) and inbox_pending == 0
    result = {
        "run_id": run_id, "passed": passed, "generated": len(generated),
        "simulator_pending": simulator_pending, "gateway_pending": gateway_pending,
        "inbox_count": inbox_count, "inbox_pending": inbox_pending, "devices": details,
        "elapsed_seconds": round(elapsed_seconds, 1), "cases_passed": len(cases) if cases_passed else 0,
        "fault_events_complete": required_events.issubset(event_names), "events": events,
    }
    (run_dir / "reconciliation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 阶段二运行 {run_id}", "", f"对账结果：{'通过' if passed else '未通过'}", "", f"实测运行 {elapsed_seconds:.1f} 秒；功能用例 {len(cases)} 条，{'全通过' if cases_passed else '未全通过'}。", "", f"生成 {len(generated)} 条；后端收件 {inbox_count} 条，待处理 {inbox_pending} 条。", "", "| 设备 | 生成 | 入库 | 缺失 | 重复 |", "| --- | ---: | ---: | ---: | ---: |"]
    for device, item in details.items():
        lines.append(f"| {device} | {item['expected']} | {item['actual']} | {len(item['missing'])} | {len(item['duplicates'])} |")
    lines += ["", f"模拟器待发 {simulator_pending}；网关待写 {gateway_pending}。", "", "| 故障/恢复事件 | UTC 时间 |", "| --- | --- |"]
    lines += [f"| {item['event']} | {item['at']} |" for item in events if item["event"] in required_events]
    lines += ["", "完整事件见 events.jsonl；接口用例执行记录见 cases.csv。", ""]
    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    # Raw stderr stays local: dependency SQL diagnostics can include credential hashes.
    for service in ("api", "gateway", "api-restart", "gateway-restart"):
        sanitized_log(run_dir / f"{service}.stderr.log", run_dir / f"{service}.safe.jsonl")
    files = ["run.json", "events.jsonl", "metrics.csv", "simulator.jsonl", "gateway.jsonl", "api.jsonl", "gateway-restart.jsonl", "api-restart.jsonl", "api.safe.jsonl", "gateway.safe.jsonl", "api-restart.safe.jsonl", "gateway-restart.safe.jsonl", "reconciliation.json", "summary.md", "cases.csv"]
    with zipfile.ZipFile(run_dir / "review-bundle.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            path = run_dir / name
            if path.is_file():
                archive.write(path, arcname=name)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
