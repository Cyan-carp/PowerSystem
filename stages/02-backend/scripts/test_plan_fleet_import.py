"""Unit tests for the read-only fleet import dry-run planner."""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).with_name("plan-fleet-import.py")
CACHE_ROOT = Path(__file__).resolve().parents[3] / ".cache"
PID = os.getpid()
_SPEC = importlib.util.spec_from_file_location("plan_fleet_import", SCRIPT)
planner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(planner)

SNAPSHOT_FIELDS = ("device_code", "station_code", "name", "dev_type", "vendor", "is_simulated")


def run_planner(*args: object) -> tuple[int, dict | None, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = planner.main([str(arg) for arg in args])
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


def snapshot_from(report: dict, fields: tuple[str, ...] = SNAPSHOT_FIELDS) -> dict:
    return {"devices": [{field: device[field] for field in fields} for device in report["planned_devices"]]}


class PlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        self._created: list[Path] = []
        self._sequence = 0

    def tearDown(self) -> None:
        for path in self._created:
            path.unlink(missing_ok=True)

    def cache_path(self, label: str) -> Path:
        self._sequence += 1
        return CACHE_ROOT / f"plan-fleet-import-{self._testMethodName}-{PID}-{self._sequence}-{label}.json"

    def write(self, label: str, payload: object) -> Path:
        path = self.cache_path(label)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self._created.append(path)
        return path

    def baseline_report(self) -> dict:
        code, report, _ = run_planner()
        self.assertEqual(code, 0)
        assert report is not None
        return report

    def test_default_plan_has_exactly_140_business_devices(self) -> None:
        report = self.baseline_report()
        summary = report["summary"]
        self.assertEqual(report["mode"], "dry-run")
        self.assertTrue(report["read_only"])
        self.assertEqual(summary["planned_total"], 140)
        self.assertEqual(summary["station_total"], 4)
        self.assertEqual(summary["create"], 140)
        self.assertEqual(summary["same"], 0)
        self.assertEqual(summary["conflict"], 0)
        self.assertFalse(summary["has_conflicts"])
        self.assertEqual(len(report["planned_devices"]), 140)
        self.assertEqual({device["action"] for device in report["planned_devices"]}, {"create"})

    def test_per_station_counts_are_48_40_32_20(self) -> None:
        report = self.baseline_report()
        counts = {station["station_code"]: station["planned"] for station in report["stations"]}
        self.assertEqual(counts, {"PV-01": 48, "PV-02": 40, "PV-03": 32, "PV-04": 20})
        self.assertEqual([station["station_code"] for station in report["stations"]], ["PV-01", "PV-02", "PV-03", "PV-04"])

    def test_baseline_devices_are_excluded_and_untouched(self) -> None:
        report = self.baseline_report()
        codes = {device["device_code"] for device in report["planned_devices"]}
        self.assertFalse(codes & {"INV-1001", "INV-1002", "INV-1003"})
        self.assertEqual(
            report["excluded_baseline"],
            [
                {
                    "station_code": "ST-01",
                    "station_name": "原三设备演示基线站",
                    "device_codes": ["INV-1001", "INV-1002", "INV-1003"],
                }
            ],
        )
        self.assertEqual(report["summary"]["excluded_baseline_devices"], 3)
        self.assertNotIn("ST-01", {device["station_code"] for device in report["planned_devices"]})
        self.assertTrue(all(device["scope"] == "business" for device in report["planned_devices"]))
        self.assertEqual(len(codes), 140)

    def test_identical_snapshot_reports_all_same(self) -> None:
        report = self.baseline_report()
        path = self.write("existing", snapshot_from(report))
        code, result, _ = run_planner("--existing", path)
        self.assertEqual(code, 0)
        assert result is not None
        self.assertEqual(result["summary"]["same"], 140)
        self.assertEqual(result["summary"]["create"], 0)
        self.assertEqual(result["summary"]["conflict"], 0)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["existing_only"], [])

    def test_partial_snapshot_compares_only_provided_fields(self) -> None:
        report = self.baseline_report()
        path = self.write("existing", snapshot_from(report, ("device_code", "station_code")))
        code, result, _ = run_planner("--existing", path)
        self.assertEqual(code, 0)
        assert result is not None
        self.assertEqual(result["summary"]["same"], 140)

    def test_conflicting_vendor_is_reported(self) -> None:
        report = self.baseline_report()
        snapshot = snapshot_from(report)
        snapshot["devices"][0]["vendor"] = "another-vendor"
        path = self.write("existing", snapshot)
        code, result, _ = run_planner("--existing", path)
        self.assertEqual(code, 2)
        assert result is not None
        self.assertEqual(result["summary"]["conflict"], 1)
        self.assertEqual(result["summary"]["create"], 0)
        self.assertEqual(result["summary"]["same"], 139)
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["device_code"], report["planned_devices"][0]["device_code"])
        self.assertEqual(
            conflict["fields"],
            [{"field": "vendor", "planned": "synthetic-fleet", "existing": "another-vendor"}],
        )
        actions = {device["device_code"]: device["action"] for device in result["planned_devices"]}
        self.assertEqual(actions[conflict["device_code"]], "conflict")
        self.assertEqual(result["stations"][0]["conflict"], 1)
        self.assertEqual(result["stations"][0]["same"], 47)

    def test_station_mismatch_is_a_conflict(self) -> None:
        report = self.baseline_report()
        snapshot = snapshot_from(report, ("device_code", "station_code"))
        snapshot["devices"][0]["station_code"] = "PV-02"
        path = self.write("existing", snapshot)
        code, result, _ = run_planner("--existing", path)
        self.assertEqual(code, 2)
        assert result is not None
        self.assertEqual(result["summary"]["conflict"], 1)
        self.assertEqual(result["conflicts"][0]["fields"][0]["field"], "station_code")

    def test_baseline_row_in_snapshot_is_existing_only(self) -> None:
        report = self.baseline_report()
        snapshot = snapshot_from(report, ("device_code", "station_code"))
        snapshot["devices"].append({"device_code": "INV-1001", "station_code": "ST-01"})
        path = self.write("existing", snapshot)
        code, result, _ = run_planner("--existing", path)
        self.assertEqual(code, 0)
        assert result is not None
        self.assertEqual(result["summary"]["conflict"], 0)
        self.assertEqual(result["summary"]["existing_only"], 1)
        self.assertEqual(result["existing_only"], ["INV-1001"])

    def test_duplicate_existing_code_is_invalid(self) -> None:
        report = self.baseline_report()
        snapshot = snapshot_from(report, ("device_code", "station_code"))
        snapshot["devices"].append(dict(snapshot["devices"][0]))
        path = self.write("existing", snapshot)
        code, result, err = run_planner("--existing", path)
        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertIn("duplicate device_code", err)

    def test_unknown_existing_field_is_invalid(self) -> None:
        snapshot = snapshot_from(self.baseline_report(), ("device_code", "station_code"))
        snapshot["devices"][0]["id"] = 1
        path = self.write("existing", snapshot)
        code, result, err = run_planner("--existing", path)
        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertIn("unknown fields", err)

    def test_missing_existing_file_is_invalid(self) -> None:
        code, result, err = run_planner("--existing", self.cache_path("missing"))
        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertIn("not found", err)

    def test_invalid_fleet_manifest_is_invalid(self) -> None:
        path = self.cache_path("fleet")
        path.write_text("{not json", encoding="utf-8")
        self._created.append(path)
        code, result, err = run_planner("--fleet", path)
        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertIn("invalid JSON", err)

    def test_unknown_argument_is_invalid(self) -> None:
        code, result, _ = run_planner("--nope")
        self.assertEqual(code, 1)
        self.assertIsNone(result)

    def test_planner_has_no_database_dependency(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(
            imported,
            {"__future__", "argparse", "json", "sys", "pathlib", "typing", "fleet"},
        )
        lowered = source.lower()
        for forbidden in ("psycopg", "sqlite3", "asyncpg", "pymysql", "sqlalchemy", "postgres", "select ", "insert into", "connect(", "cursor("):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
