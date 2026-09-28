"""Audit selected OpenCEM CSV partitions before any fault-onset claims."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("pv1volt", "pv1curr", "pv1power", "outsumw", "temper1", "temper2")


def audit_file(path: Path) -> dict[str, object]:
    widths: Counter[int] = Counter()
    inverter_times: dict[str, list[int]] = defaultdict(list)
    missing: Counter[str] = Counter()
    valid = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream)
        header = next(rows)
        for row in rows:
            widths[len(row)] += 1
            if len(row) != len(header):
                continue
            values = dict(zip(header, row))
            try:
                timestamp = int(values["read_ts"])
            except (ValueError, KeyError):
                continue
            valid += 1
            inverter_times[values["inverter"]].append(timestamp)
            missing.update(name for name in FIELDS if values.get(name, "") == "")
    times = [timestamp for series in inverter_times.values() for timestamp in series]
    intervals = [b - a for series in inverter_times.values()
                 for a, b in zip(sorted(series), sorted(series)[1:])]
    def utc(value: int) -> str:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "file": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest(),
        "header_columns": len(header), "row_width_counts": dict(sorted(widths.items())),
        "parseable_rows": valid, "inverter_rows": {key: len(value) for key, value in inverter_times.items()},
        "first_utc": utc(min(times)) if times else None,
        "last_utc": utc(max(times)) if times else None,
        "median_interval_seconds": statistics.median(intervals) if intervals else None,
        "missing_selected_fields": {name: missing[name] for name in FIELDS},
        "fault_label_columns": [name for name in header if "fault" in name.lower() or "alarm" in name.lower()],
    }


def audit_support(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        dictionary = json.loads(archive.read("opencem-v1.0.0/documentation/column_dictionary.json"))
        fault_columns = [item for item in dictionary["columns"] if item["table"] == "fault_history"]
        payload = [item for item in fault_columns if item["column"].startswith("faulthistoryrecord")
                   and not item["column"].endswith("_time")]
        event_time = [item for item in fault_columns if item["column"].endswith("_time")]
        license_text = archive.read("opencem-v1.0.0/LICENSE-DATA.txt").decode("utf-8")
    return {
        "file": path.name, "bytes": path.stat().st_size,
        "payload_register_columns": len(payload), "decoded_event_time_columns": len(event_time),
        "payload_semantics": "component-specific according to the source column dictionary",
        "license_cc_by_4_0": "CC BY 4.0" in license_text,
        "event_truth_verified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--support-zip", type=Path)
    args = parser.parse_args()
    result = {
        "source": "https://github.com/OpenCEM-platform/opencem-dataset",
        "source_revision": "5884d253a5267fb240b7a8df6fa9e4d49a905167",
        "data_license": "CC BY 4.0 (source metadata and README)",
        "partitions": [audit_file(path) for path in args.files],
        "one_hour_risk_validated": False,
        "reason": "These measurement partitions have no documented fault onset labels or event ledger.",
    }
    if args.support_zip:
        result["support_package"] = audit_support(args.support_zip)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["partitions"], ensure_ascii=False))


if __name__ == "__main__":
    main()
