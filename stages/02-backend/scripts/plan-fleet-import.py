#!/usr/bin/env python3
"""Read-only dry-run planner for the 4-station / 140-device fleet expansion.

The planner reads the single fleet manifest through ``fleet.py`` (the same
loader, validator and expander used by the simulator), keeps only
``scope=business`` rows, and prints a JSON plan for human review. It never
connects to a database, never writes to the manifest and never imports a
database driver.

Optional ``--existing`` points at an offline JSON snapshot of the devices that
already exist. Only the fields present in the snapshot are compared, so a
minimal export containing ``device_code`` and ``station_code`` is valid.

Existing snapshot shape::

    {
      "devices": [
        {
          "device_code": "PV-01-INV-01",
          "station_code": "PV-01",
          "name": "示范园区 A 屋顶光伏片区 01号逆变器",
          "dev_type": "inverter",
          "vendor": "synthetic-fleet",
          "is_simulated": true
        }
      ]
    }

Exit codes: 0 = no conflicts, 2 = conflicts found, 1 = invalid input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SIMULATOR_DIR = Path(__file__).resolve().parents[2] / "01-data-chain" / "simulator"
if str(SIMULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_DIR))

from fleet import CODE_PATTERN, Fleet, FleetError, expand_devices, load_fleet  # noqa: E402

EXISTING_KEYS = frozenset({"devices"})
EXISTING_DEVICE_KEYS = frozenset(
    {"device_code", "station_code", "name", "dev_type", "vendor", "is_simulated"}
)
COMPARED_FIELDS = ("station_code", "name", "dev_type", "vendor", "is_simulated")


class PlanError(ValueError):
    """Raised when the offline existing-device snapshot is malformed."""


class _ArgumentParser(argparse.ArgumentParser):
    """Argparse variant that reports usage errors as invalid input."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise PlanError(f"{self.prog}: {message}")


def load_existing(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load an offline existing-device snapshot keyed by ``device_code``."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PlanError(f"{source}: existing snapshot not found") from exc
    except OSError as exc:
        raise PlanError(f"{source}: cannot read existing snapshot: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanError(f"{source}: invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise PlanError(f"{source}: top level must be a JSON object")
    unknown = sorted(set(data) - EXISTING_KEYS)
    if unknown:
        raise PlanError(f"{source}: unknown top-level fields: {unknown}")
    if "devices" not in data:
        raise PlanError(f"{source}: missing top-level field: devices")
    raw_devices = data["devices"]
    if not isinstance(raw_devices, list):
        raise PlanError(f"{source}: devices must be a list")

    devices: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_devices):
        label = f"devices[{index}]"
        if not isinstance(raw, dict):
            raise PlanError(f"{source}: {label} must be a JSON object")
        unknown = sorted(set(raw) - EXISTING_DEVICE_KEYS)
        if unknown:
            raise PlanError(f"{source}: {label} has unknown fields: {unknown}")
        code = _snapshot_code(raw, "device_code", label, source)
        if code in devices:
            raise PlanError(f"{source}: duplicate device_code: {code}")
        device: dict[str, Any] = {
            "device_code": code,
            "station_code": _snapshot_code(raw, "station_code", label, source),
        }
        for field in ("name", "dev_type", "vendor"):
            if field in raw:
                value = raw[field]
                if not isinstance(value, str) or not value.strip():
                    raise PlanError(f"{source}: {label}.{field} must be a non-empty string")
                device[field] = value
        if "is_simulated" in raw:
            value = raw["is_simulated"]
            if not isinstance(value, bool):
                raise PlanError(f"{source}: {label}.is_simulated must be a boolean")
            device["is_simulated"] = value
        devices[code] = device
    return devices


def build_report(
    fleet: Fleet,
    existing: dict[str, dict[str, Any]] | None = None,
    *,
    fleet_source: Path | None = None,
    existing_source: Path | None = None,
) -> dict[str, Any]:
    """Build the dry-run report; only ``scope=business`` rows are planned."""
    expanded = expand_devices(fleet)
    planned_rows = [row for row in expanded if row["scope"] == "business"]
    baseline_rows = [row for row in expanded if row["scope"] != "business"]
    existing = existing or {}

    counters = {
        station.station_code: {"planned": 0, "create": 0, "same": 0, "conflict": 0}
        for station in fleet.business_stations
    }
    planned_devices: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for row in planned_rows:
        station_counts = counters[row["station_code"]]
        station_counts["planned"] += 1
        device = dict(row)
        snapshot = existing.get(row["device_code"])
        if snapshot is None:
            action = "create"
        else:
            differences = [
                {"field": field, "planned": row.get(field), "existing": snapshot[field]}
                for field in COMPARED_FIELDS
                if field in snapshot and snapshot[field] != row.get(field)
            ]
            if differences:
                action = "conflict"
                conflicts.append(
                    {
                        "device_code": row["device_code"],
                        "station_code": row["station_code"],
                        "fields": differences,
                    }
                )
            else:
                action = "same"
        device["action"] = action
        planned_devices.append(device)
        station_counts[action] += 1

    planned_codes = {row["device_code"] for row in planned_rows}
    existing_only = sorted(code for code in existing if code not in planned_codes)

    baseline: list[dict[str, Any]] = []
    for station in fleet.baseline_stations:
        baseline.append(
            {
                "station_code": station.station_code,
                "station_name": station.station_name,
                "device_codes": [row["device_code"] for row in baseline_rows if row["station_code"] == station.station_code],
            }
        )

    summary = {
        "planned_total": len(planned_rows),
        "station_total": len(fleet.business_stations),
        "create": sum(counts["create"] for counts in counters.values()),
        "same": sum(counts["same"] for counts in counters.values()),
        "conflict": sum(counts["conflict"] for counts in counters.values()),
        "existing_only": len(existing_only),
        "excluded_baseline_devices": len(baseline_rows),
        "has_conflicts": bool(conflicts),
    }

    return {
        "mode": "dry-run",
        "read_only": True,
        "fleet": {
            "fleet_id": fleet.fleet_id,
            "schema_version": fleet.schema_version,
            "source": str(fleet_source) if fleet_source else None,
        },
        "existing": {
            "source": str(existing_source) if existing_source else None,
            "devices": len(existing),
        },
        "summary": summary,
        "stations": [
            {
                "station_code": station.station_code,
                "station_name": station.station_name,
                "planned": counters[station.station_code]["planned"],
                "create": counters[station.station_code]["create"],
                "same": counters[station.station_code]["same"],
                "conflict": counters[station.station_code]["conflict"],
            }
            for station in fleet.business_stations
        ],
        "excluded_baseline": baseline,
        "planned_devices": planned_devices,
        "conflicts": conflicts,
        "existing_only": existing_only,
    }


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(
        description="Print a read-only import plan for the business fleet (JSON).",
        epilog="Exit codes: 0 no conflicts, 2 conflicts, 1 invalid input.",
    )
    parser.add_argument(
        "--fleet",
        type=Path,
        default=None,
        help="fleet manifest path (default: simulator/fleet.json)",
    )
    parser.add_argument(
        "--existing",
        type=Path,
        default=None,
        help="offline existing-device snapshot in JSON",
    )
    try:
        args = parser.parse_args(argv)
        fleet = load_fleet(args.fleet)
        existing = load_existing(args.existing) if args.existing is not None else None
    except (FleetError, PlanError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    report = build_report(
        fleet,
        existing,
        fleet_source=args.fleet,
        existing_source=args.existing,
    )
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 2 if report["summary"]["has_conflicts"] else 0


def _snapshot_code(raw: dict[str, Any], field: str, label: str, source: Path) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not CODE_PATTERN.fullmatch(value):
        raise PlanError(f"{source}: {label}.{field} must match [A-Za-z0-9_-]{{1,64}}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
