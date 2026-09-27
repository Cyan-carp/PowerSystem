"""Compare the simulator ledger with every TDengine row and export query evidence."""

from __future__ import annotations

import base64
import csv
import json
import os
import sqlite3
import sys
import urllib.request
from collections import Counter
from pathlib import Path


def query(sql: str, password: str) -> list[list[object]]:
    auth = base64.b64encode(f"root:{password}".encode()).decode()
    request = urllib.request.Request(
        "http://127.0.0.1:6041/rest/sql",
        data=sql.encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "text/plain"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if result.get("code") != 0:
        raise RuntimeError(f"TDengine code={result.get('code')} desc={result.get('desc')}")
    return result.get("data", [])


run_dir = Path(sys.argv[1])
database = sys.argv[2]
password = os.environ["TDENGINE_ROOT_PASSWORD"]
expected_samples = int(sys.argv[3])
devices = ("INV-1001", "INV-1002", "INV-1003")
ledger_path = run_dir / "simulator.sqlite"
generated: dict[str, dict[int, int]] = {device: {} for device in devices}
simulator_pending = -1
gateway_pending = -1
if ledger_path.exists():
    with sqlite3.connect(f"file:{ledger_path.as_posix()}?mode=ro", uri=True) as db:
        for device, seq, ts in db.execute("SELECT device_id,seq,ts_ms FROM generated"):
            if device in generated:
                generated[device][int(seq)] = int(ts)
        simulator_pending = int(db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
gateway_path = run_dir / "gateway.sqlite"
if gateway_path.exists():
    with sqlite3.connect(f"file:{gateway_path.as_posix()}?mode=ro", uri=True) as db:
        gateway_pending = int(db.execute("SELECT COUNT(*) FROM pending").fetchone()[0])

report: dict[str, object] = {
    "database": database,
    "expected_per_device": expected_samples,
    "expected_total": expected_samples * len(devices),
    "simulator_pending": simulator_pending,
    "gateway_pending": gateway_pending,
    "devices": {},
    "td_error": None,
}
all_rows: list[list[object]] = []
try:
    for device in devices:
        table = "t_inv_" + device.removeprefix("INV-")
        rows = query(
            f"SELECT CAST(ts AS BIGINT),seq,voltage,current,temperature,power,status,fault_code FROM {database}.{table} ORDER BY ts",
            password,
        )
        actual = [(int(row[1]), int(row[0])) for row in rows]
        actual_by_seq = {seq: ts for seq, ts in actual}
        generated_by_seq = generated[device]
        missing = sorted(set(generated_by_seq) - set(actual_by_seq))
        extra = sorted(set(actual_by_seq) - set(generated_by_seq))
        expected_seq = set(range(expected_samples))
        missing_expected_seq = sorted(expected_seq - set(actual_by_seq))
        wrong_ts = sorted(seq for seq in set(generated_by_seq) & set(actual_by_seq) if generated_by_seq[seq] != actual_by_seq[seq])
        duplicate_seq = sorted(seq for seq, count in Counter(seq for seq, _ in actual).items() if count > 1)
        report["devices"][device] = {
            "generated": len(generated_by_seq),
            "in_tdengine": len(rows),
            "missing_seq": missing,
            "missing_expected_seq": missing_expected_seq,
            "extra_seq": extra,
            "wrong_timestamp_seq": wrong_ts,
            "duplicate_seq": duplicate_seq,
        }
        all_rows.extend([[device, *row] for row in rows])
    report["generated_total"] = sum(len(values) for values in generated.values())
    report["tdengine_total"] = len(all_rows)
    report["passed"] = (
        all(len(generated[device]) == expected_samples for device in devices)
        and len(all_rows) == expected_samples * len(devices)
        and simulator_pending == 0
        and gateway_pending == 0
        and all(not details["missing_seq"] and not details["missing_expected_seq"] and not details["extra_seq"] and not details["wrong_timestamp_seq"] and not details["duplicate_seq"] for details in report["devices"].values())
    )
except Exception as exc:
    report["td_error"] = str(exc)
    report["passed"] = False

with (run_dir / "reconciliation.json").open("w", encoding="utf-8") as target:
    json.dump(report, target, indent=2, ensure_ascii=False)
with (run_dir / "telemetry.csv").open("w", encoding="utf-8", newline="") as target:
    writer = csv.writer(target)
    writer.writerow(("device_id", "ts_ms", "seq", "voltage_v", "current_a", "temperature_c", "power_kw", "status", "fault_code"))
    writer.writerows(all_rows)
print(json.dumps({"passed": report["passed"], "generated_total": report.get("generated_total", 0), "tdengine_total": report.get("tdengine_total", 0), "td_error": report["td_error"]}, ensure_ascii=False))
