#!/usr/bin/env python3
"""Render an idempotent PostgreSQL import script for the business fleet.

The renderer reuses ``stages/01-data-chain/simulator/fleet.py`` (the same
``load_fleet`` / ``expand_devices`` used by the simulator) and emits plain SQL
text for ``psql``. It never opens a database connection, never imports a
driver, never reads secrets and never touches the manifest.

Only ``scope=business`` rows are rendered. Baseline rows (``ST-01`` and its
three original devices) are dropped before rendering, so the generated script
cannot rewrite, rename, re-vendor or soft-delete an existing baseline device.

Safety contract of the generated script:

* one transaction; any conflict aborts everything;
* ``LOCK TABLE devices IN SHARE ROW EXCLUSIVE MODE`` with a 5 s lock timeout;
* an existing ``device_code`` whose identity differs (or which is soft-deleted)
  raises an exception instead of being overwritten;
* new codes are inserted with ``ON CONFLICT (device_code) DO NOTHING``;
* after the insert every expected code must exist and must not be soft-deleted;
* no ``UPDATE devices``, no ``DELETE``, no schema change to the application
  tables. The staging table is ``TEMP`` and ``ON COMMIT DROP``.

Usage::

    python stages/02-backend/scripts/render-fleet-import.py > import.sql
    python stages/02-backend/scripts/render-fleet-import.py --output import.sql
    python stages/02-backend/scripts/render-fleet-import.py \\
        --fleet stages/01-data-chain/simulator/fixtures/fleet-smoke-7.json \\
        --output .cache/smoke-import.sql

Exit codes: 0 = rendered, 1 = invalid input (bad manifest, existing output).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

SIMULATOR_DIR = Path(__file__).resolve().parents[2] / "01-data-chain" / "simulator"
if str(SIMULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_DIR))

from fleet import Fleet, FleetError, expand_devices, load_fleet  # noqa: E402

TABLE = "devices"
STAGING_TABLE = "fleet_import_expected"

# Column order used by the staging table, the INSERT and the conflict check.
COLUMNS = (
    "device_code",
    "name",
    "dev_type",
    "vendor",
    "is_simulated",
    "reference_vendor",
    "reference_model",
    "station_code",
    "group_name",
)

# Identity fields compared for an existing device_code. ``deleted_at`` is
# checked separately because it is not part of the imported row.
IDENTITY_FIELDS = (
    "name",
    "dev_type",
    "vendor",
    "is_simulated",
    "station_code",
    "group_name",
    "reference_vendor",
    "reference_model",
)

# Text columns where an empty string and NULL both mean "empty".
NULLABLE_TEXT_FIELDS = frozenset({"vendor", "group_name", "reference_vendor", "reference_model"})


class RenderError(ValueError):
    """Raised when the manifest cannot be rendered or the output is unsafe."""


class _ArgumentParser(argparse.ArgumentParser):
    """Argparse variant that reports usage errors as invalid input."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise RenderError(f"{self.prog}: {message}")


def business_rows(fleet: Fleet) -> list[dict[str, Any]]:
    """Return normalized ``scope=business`` rows, in manifest order."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in expand_devices(fleet):
        if row.get("scope") != "business":
            continue
        code = row["device_code"]
        if code in seen:
            raise RenderError(f"duplicate business device_code: {code}")
        seen.add(code)
        rows.append(
            {
                "device_code": code,
                "name": _required_text(row.get("name"), "name", code),
                "dev_type": _required_text(row.get("dev_type"), "dev_type", code),
                "vendor": _optional_text(row.get("vendor"), "vendor", code),
                "is_simulated": _required_bool(row.get("is_simulated"), code),
                "reference_vendor": _optional_text(row.get("reference_vendor"), "reference_vendor", code),
                "reference_model": _optional_text(row.get("reference_model"), "reference_model", code),
                "station_code": _required_text(row.get("station_code"), "station_code", code),
                "group_name": _optional_text(row.get("group_name"), "group_name", code),
            }
        )
    if not rows:
        raise RenderError("fleet manifest has no scope=business devices to import")
    return rows


def sql_literal(value: Any) -> str:
    """Render a SQL literal; ``None`` becomes ``NULL`` and quotes are doubled."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    text = str(value)
    if "\x00" in text:
        raise RenderError("device text contains a NUL byte, which PostgreSQL cannot store")
    return "'" + text.replace("'", "''") + "'"


def render_sql(fleet: Fleet, *, fleet_source: str | Path | None = None) -> str:
    """Render the complete import script for the given fleet."""
    rows = business_rows(fleet)
    stations = [station for station in fleet.business_stations]
    station_counts = {
        station.station_code: sum(1 for row in rows if row["station_code"] == station.station_code)
        for station in stations
    }

    parts = [
        _header(fleet, rows, station_counts, fleet_source),
        "\\set ON_ERROR_STOP on",
        "",
        "BEGIN;",
        "",
        "-- Fail fast instead of queueing behind a long-running writer.",
        "SET LOCAL lock_timeout = '5s';",
        "-- Backslashes are literal and quotes are doubled: one escaping rule everywhere.",
        "SET LOCAL standard_conforming_strings = on;",
        "",
        "-- Block concurrent writers while existing rows are compared and new rows inserted.",
        f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE;",
        "",
        "-- Session-local staging table; dropped by COMMIT/ROLLBACK, no schema change.",
        f"CREATE TEMP TABLE {STAGING_TABLE} (",
        "  device_code      VARCHAR(64) PRIMARY KEY,",
        "  name             VARCHAR(128) NOT NULL,",
        "  dev_type         VARCHAR(32) NOT NULL,",
        "  vendor           VARCHAR(64),",
        "  is_simulated     BOOLEAN NOT NULL,",
        "  reference_vendor VARCHAR(64),",
        "  reference_model  VARCHAR(64),",
        "  station_code     VARCHAR(64) NOT NULL,",
        "  group_name       VARCHAR(64),",
        "  is_existing      BOOLEAN NOT NULL DEFAULT FALSE",
        ") ON COMMIT DROP;",
        "",
        f"INSERT INTO {STAGING_TABLE} ({', '.join(COLUMNS)}) VALUES",
        ",\n".join(_values_row(row) for row in rows) + ";",
        "",
        "-- Snapshot which codes already exist, before anything is inserted.",
        f"UPDATE {STAGING_TABLE} e",
        "   SET is_existing = TRUE",
        f"  FROM {TABLE} d",
        " WHERE d.device_code = e.device_code;",
        "",
        _conflict_check_sql(),
        "",
        "-- Existing rows are never updated; identical rows are left untouched.",
        f"INSERT INTO {TABLE} ({', '.join(COLUMNS)})",
        f"SELECT {', '.join(COLUMNS)}",
        f"  FROM {STAGING_TABLE}",
        " ORDER BY device_code",
        "ON CONFLICT (device_code) DO NOTHING",
        "RETURNING device_code;",
        "",
        _verification_sql(),
        "",
        "-- Result for the run log: expected / already present / newly inserted.",
        "SELECT count(*) AS expected_total,",
        "       count(*) FILTER (WHERE is_existing) AS existing_total,",
        "       count(*) FILTER (WHERE NOT is_existing) AS inserted_total",
        f"  FROM {STAGING_TABLE};",
        "",
        "COMMIT;",
        "",
    ]
    return "\n".join(parts)


def _header(
    fleet: Fleet,
    rows: list[dict[str, Any]],
    station_counts: dict[str, int],
    fleet_source: str | Path | None,
) -> str:
    station_text = ", ".join(f"{code}={count}" for code, count in station_counts.items())
    source_text = _comment_safe(str(fleet_source)) if fleet_source is not None else "default fleet.json"
    return "\n".join(
        [
            "-- PowerSystem fleet import: scope=business only.",
            f"-- fleet_id: {_comment_safe(fleet.fleet_id)}",
            f"-- manifest: {source_text}",
            f"-- rows: {len(rows)} business devices across {len(station_counts)} stations ({station_text})",
            "-- baseline devices are excluded and never touched.",
            "--",
            "-- Contract:",
            "--   * existing device_code with a different identity, or soft-deleted, aborts everything;",
            "--   * identical device_code is left as-is (ON CONFLICT DO NOTHING);",
            "--   * no UPDATE devices, no DELETE, no schema change to application tables.",
            "--",
            "-- Run with: psql \"$DATABASE_URL\" -f <this file>",
        ]
    )


def _comment_safe(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ")


def _values_row(row: dict[str, Any]) -> str:
    values = [
        sql_literal(row["device_code"]),
        sql_literal(row["name"]),
        sql_literal(row["dev_type"]),
        sql_literal(row["vendor"]),
        sql_literal(row["is_simulated"]),
        sql_literal(row["reference_vendor"]),
        sql_literal(row["reference_model"]),
        sql_literal(row["station_code"]),
        sql_literal(row["group_name"]),
    ]
    return "  (" + ", ".join(values) + ")"


def _conflict_check_sql() -> str:
    checks = ["               CASE WHEN d.deleted_at IS NOT NULL THEN 'soft-deleted' END"]
    for field in IDENTITY_FIELDS:
        if field in NULLABLE_TEXT_FIELDS:
            comparison = f"NULLIF(d.{field}, '') IS DISTINCT FROM NULLIF(e.{field}, '')"
        else:
            comparison = f"d.{field} IS DISTINCT FROM e.{field}"
        checks.append(f"               CASE WHEN {comparison} THEN '{field}' END")
    check_block = ",\n".join(checks)
    return "\n".join(
        [
            "-- Abort on the first conflicting existing device; nothing is overwritten.",
            "DO $fleet_check$",
            "DECLARE",
            "  v_conflict_count INTEGER;",
            "  v_conflict_detail TEXT;",
            "BEGIN",
            "  SELECT count(*),",
            "         string_agg(e.device_code || ' [' || e.reasons || ']', ', ' ORDER BY e.device_code)",
            "    INTO v_conflict_count, v_conflict_detail",
            "    FROM (",
            "      SELECT e.device_code,",
            "             concat_ws(",
            "               '; ',",
            check_block,
            "             ) AS reasons",
            f"        FROM {STAGING_TABLE} e",
            f"        JOIN {TABLE} d ON d.device_code = e.device_code",
            "    ) e",
            "   WHERE e.reasons <> '';",
            "",
            "  IF v_conflict_count > 0 THEN",
            "    RAISE EXCEPTION 'fleet import aborted: % existing device(s) conflict: %',",
            "      v_conflict_count, v_conflict_detail",
            "      USING HINT = 'No device was changed; the transaction is rolled back. Reconcile them manually.';",
            "  END IF;",
            "END;",
            "$fleet_check$;",
        ]
    )


def _verification_sql() -> str:
    return "\n".join(
        [
            "-- Every expected code must now exist and must not be soft-deleted.",
            "DO $fleet_verify$",
            "DECLARE",
            "  v_missing TEXT;",
            "BEGIN",
            "  SELECT string_agg(e.device_code, ', ' ORDER BY e.device_code)",
            "    INTO v_missing",
            f"    FROM {STAGING_TABLE} e",
            f"    LEFT JOIN {TABLE} d",
            "      ON d.device_code = e.device_code",
            "     AND d.deleted_at IS NULL",
            "   WHERE d.device_code IS NULL;",
            "",
            "  IF v_missing IS NOT NULL THEN",
            "    RAISE EXCEPTION 'fleet import verification failed: missing or soft-deleted devices: %', v_missing;",
            "  END IF;",
            "END;",
            "$fleet_verify$;",
        ]
    )


def _required_text(value: Any, field: str, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise RenderError(f"{code}: {field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str, code: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RenderError(f"{code}: {field} must be a string or null")
    return value


def _required_bool(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise RenderError(f"{code}: is_simulated must be a boolean")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(
        description="Render the business fleet import SQL for psql (no database access).",
        epilog="Exit codes: 0 rendered, 1 invalid input.",
    )
    parser.add_argument(
        "--fleet",
        type=Path,
        default=None,
        help="fleet manifest path (default: simulator/fleet.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write SQL to a new file (default: stdout); existing files are never overwritten",
    )
    try:
        args = parser.parse_args(argv)
        fleet = load_fleet(args.fleet)
        sql = render_sql(fleet, fleet_source=args.fleet)
        if args.output is None:
            sys.stdout.write(sql)
        else:
            _write_new_file(args.output, sql)
            print(f"rendered {len(business_rows(fleet))} business devices -> {args.output}", file=sys.stderr)
    except (FleetError, RenderError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _write_new_file(path: Path, text: str) -> None:
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise RenderError(f"{path}: output file already exists; refusing to overwrite") from exc


if __name__ == "__main__":
    raise SystemExit(main())
