"""Unit tests for the business-fleet PostgreSQL import renderer."""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).with_name("render-fleet-import.py")
CACHE_ROOT = Path(__file__).resolve().parents[3] / ".cache"
PID = os.getpid()
SIMULATOR_DIR = Path(__file__).resolve().parents[2] / "01-data-chain" / "simulator"
SMOKE_FIXTURE = SIMULATOR_DIR / "fixtures" / "fleet-smoke-7.json"
if str(SIMULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_DIR))

from fleet import parse_fleet  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("render_fleet_import", SCRIPT)
renderer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(renderer)

VALUES_ROW = re.compile(r"^  \('", re.M)
DEVICE_CODE = re.compile(r"'([A-Za-z0-9_-]+-INV-\d{2})'")


def run_renderer(*args: object) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = renderer.main([str(arg) for arg in args])
    return code, out.getvalue(), err.getvalue()


def quote_manifest(station_name: str) -> dict:
    return {
        "schema_version": 1,
        "fleet_id": "fleet-quotes",
        "sampling_seconds": 5,
        "stations": [
            {
                "station_code": "PV-09",
                "station_name": station_name,
                "scope": "business",
                "kind": "pv",
                "device_prefix": "INV",
                "device_count": 1,
                "rated_power_kw": 100,
                "profile": "generic-pv-100kw",
                "daylight_mode": "real",
                "group_size": 8,
            }
        ],
    }


class RendererTest(unittest.TestCase):
    def setUp(self) -> None:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        self._created: list[Path] = []
        self._sequence = 0

    def tearDown(self) -> None:
        for path in self._created:
            path.unlink(missing_ok=True)

    def cache_path(self, label: str, suffix: str = ".sql") -> Path:
        self._sequence += 1
        return CACHE_ROOT / f"render-fleet-import-{self._testMethodName}-{PID}-{self._sequence}-{label}{suffix}"

    def render_default(self) -> str:
        code, sql, err = run_renderer()
        self.assertEqual(code, 0, err)
        return sql

    def test_default_fleet_renders_140_business_rows_without_baseline(self) -> None:
        sql = self.render_default()
        self.assertEqual(len(VALUES_ROW.findall(sql)), 140)
        self.assertEqual(len(set(DEVICE_CODE.findall(sql))), 140)
        stations = {code.split("-INV-")[0] for code in DEVICE_CODE.findall(sql)}
        self.assertEqual(stations, {"PV-01", "PV-02", "PV-03", "PV-04"})
        self.assertNotIn("ST-01", sql)
        for code in ("INV-1001", "INV-1002", "INV-1003"):
            self.assertNotIn(code, sql)
        self.assertIn("PV-01=48, PV-02=40, PV-03=32, PV-04=20", sql)
        self.assertIn("default fleet.json", sql)

    def test_smoke_fixture_renders_4_business_rows(self) -> None:
        code, sql, err = run_renderer("--fleet", SMOKE_FIXTURE)
        self.assertEqual(code, 0, err)
        self.assertEqual(VALUES_ROW.findall(sql).__len__(), 4)
        self.assertEqual(
            set(DEVICE_CODE.findall(sql)),
            {"PV-01-INV-01", "PV-01-INV-02", "PV-04-INV-01", "PV-04-INV-02"},
        )
        self.assertNotIn("ST-01", sql)
        self.assertIn("PV-01=2, PV-04=2", sql)

    def test_literals_escape_quotes_and_backslashes_and_null_references(self) -> None:
        fleet = parse_fleet(quote_manifest("O'Brien \\ 站"), source="<test>")
        sql = renderer.render_sql(fleet)
        self.assertIn("'O''Brien \\ 站 01号逆变器'", sql)
        self.assertNotIn("O'Brien \\ 站", sql)
        self.assertEqual(sql.count(", NULL, NULL, 'PV-09'"), 1)
        self.assertIn("TRUE, NULL, NULL, 'PV-09', 'PV-09-01区')", sql)

    def test_empty_string_is_normalised_to_null(self) -> None:
        self.assertEqual(renderer.sql_literal(None), "NULL")
        self.assertEqual(renderer.sql_literal(""), "''")
        self.assertIsNone(renderer._optional_text("", "vendor", "X"))
        self.assertIsNone(renderer._optional_text(None, "vendor", "X"))
        self.assertEqual(renderer.sql_literal("a'b"), "'a''b'")
        self.assertEqual(renderer.sql_literal(False), "FALSE")

    def test_existing_device_conflict_aborts_instead_of_overwriting(self) -> None:
        sql = self.render_default()
        for field in renderer.IDENTITY_FIELDS:
            if field in renderer.NULLABLE_TEXT_FIELDS:
                expected = f"NULLIF(d.{field}, '') IS DISTINCT FROM NULLIF(e.{field}, '')"
            else:
                expected = f"d.{field} IS DISTINCT FROM e.{field}"
            self.assertIn(expected, sql)
        self.assertIn("CASE WHEN d.deleted_at IS NOT NULL THEN 'soft-deleted' END", sql)
        self.assertIn("RAISE EXCEPTION 'fleet import aborted", sql)
        self.assertIn("USING HINT", sql)
        self.assertIn("ON CONFLICT (device_code) DO NOTHING", sql)
        statements = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
        self.assertNotRegex(statements, r"(?i)UPDATE\s+devices")
        self.assertNotRegex(statements, r"(?i)DELETE\s+FROM")
        self.assertNotRegex(statements, r"(?i)ALTER\s+TABLE")
        self.assertNotRegex(statements, r"(?i)DROP\s+TABLE")
        self.assertNotIn("TRUNCATE", statements)

    def test_transaction_wraps_lock_check_insert_verify_and_commit(self) -> None:
        sql = self.render_default()
        order = [
            "\\set ON_ERROR_STOP on",
            "BEGIN;",
            "SET LOCAL lock_timeout = '5s';",
            "LOCK TABLE devices IN SHARE ROW EXCLUSIVE MODE;",
            "CREATE TEMP TABLE fleet_import_expected",
            "INSERT INTO fleet_import_expected",
            "DO $fleet_check$",
            "INSERT INTO devices",
            "DO $fleet_verify$",
            "SELECT count(*) AS expected_total",
            "COMMIT;",
        ]
        positions = [sql.index(token) for token in order]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(sql.rstrip().endswith("COMMIT;"))
        self.assertEqual(sql.count("BEGIN;"), 1)
        self.assertEqual(sql.count("COMMIT;"), 1)
        self.assertIn("ON COMMIT DROP", sql)
        self.assertIn("AND d.deleted_at IS NULL", sql)

    def test_output_must_be_a_new_file(self) -> None:
        sql = self.render_default()
        existing = self.cache_path("existing")
        existing.write_text("-- keep me\n", encoding="utf-8")
        self._created.append(existing)
        code, out, err = run_renderer("--output", existing)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("already exists", err)
        self.assertEqual(existing.read_text(encoding="utf-8"), "-- keep me\n")

        fresh = self.cache_path("fresh")
        code, out, err = run_renderer("--output", fresh)
        self.assertEqual(code, 0, err)
        self._created.append(fresh)
        self.assertEqual(out, "")
        self.assertEqual(fresh.read_text(encoding="utf-8"), sql)
        self.assertIn("140 business devices", err)

    def test_invalid_fleet_manifest_exits_1(self) -> None:
        path = self.cache_path("fleet", ".json")
        path.write_text("{not json", encoding="utf-8")
        self._created.append(path)
        code, out, err = run_renderer("--fleet", path)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid JSON", err)
        self.assertEqual(run_renderer("--nope")[0], 1)

    def test_renderer_has_no_database_dependency(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(
            imported,
            {"__future__", "argparse", "sys", "pathlib", "typing", "fleet"},
        )


if __name__ == "__main__":
    unittest.main()
