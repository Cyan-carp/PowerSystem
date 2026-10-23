import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fleet import DEFAULT_FLEET_PATH, FleetError, expand_devices, load_fleet, parse_fleet


class FleetManifestTests(unittest.TestCase):
    def setUp(self):
        self.fleet = load_fleet()
        self.devices = expand_devices(self.fleet)
        self.business = [device for device in self.devices if device["scope"] == "business"]
        self.baseline = [device for device in self.devices if device["scope"] == "baseline"]

    def test_manifest_header(self):
        self.assertEqual(self.fleet.schema_version, 1)
        self.assertEqual(self.fleet.fleet_id, "pv-fleet-140-v1")
        self.assertEqual(self.fleet.sampling_seconds, 5)

    def test_station_distribution(self):
        counts = {station.station_code: station.device_total for station in self.fleet.stations}
        self.assertEqual(counts, {"PV-01": 48, "PV-02": 40, "PV-03": 32, "PV-04": 20, "ST-01": 3})
        self.assertEqual(len(self.business), 140)
        self.assertEqual(len(self.baseline), 3)
        self.assertEqual(len(self.devices), 143)

    def test_business_station_metadata(self):
        names = {
            "PV-01": "示范园区 A 屋顶光伏片区",
            "PV-02": "示范园区 B 屋顶光伏片区",
            "PV-03": "示范园区 C 地面光伏场站",
            "PV-04": "示范园区 D 屋顶光伏场站",
        }
        self.assertEqual([station.station_code for station in self.fleet.business_stations], list(names))
        for station in self.fleet.business_stations:
            self.assertEqual(station.station_name, names[station.station_code])
            self.assertEqual(station.scope, "business")
            self.assertEqual(station.kind, "pv")
            self.assertEqual(station.device_prefix, "INV")
            self.assertEqual(station.rated_power_kw, 100)
            self.assertEqual(station.profile, "generic-pv-100kw")
            self.assertEqual(station.daylight_mode, "real")
            self.assertEqual(station.group_size, 8)

    def test_baseline_station_metadata(self):
        self.assertEqual(len(self.fleet.baseline_stations), 1)
        station = self.fleet.baseline_stations[0]
        self.assertEqual(station.station_code, "ST-01")
        self.assertEqual(station.station_name, "原三设备演示基线站")
        self.assertEqual(station.scope, "baseline")
        self.assertEqual(station.kind, "pv")
        self.assertEqual(station.explicit, ("INV-1001", "INV-1002", "INV-1003"))
        self.assertEqual(station.device_total, 3)
        self.assertEqual(station.daylight_mode, "loop")
        self.assertEqual(station.daylight_loop_minutes, 60)
        self.assertIsNone(station.device_prefix)
        self.assertIsNone(station.profile)

    def test_business_device_fields(self):
        required = {
            "device_code",
            "name",
            "dev_type",
            "vendor",
            "is_simulated",
            "reference_vendor",
            "reference_model",
            "station_code",
            "group_name",
            "rated_power_kw",
            "profile",
            "scope",
        }
        for device in self.business:
            self.assertLessEqual(required, set(device))
            self.assertEqual(device["dev_type"], "inverter")
            self.assertEqual(device["vendor"], "synthetic-fleet")
            self.assertIs(device["is_simulated"], True)
            self.assertIsNone(device["reference_vendor"])
            self.assertIsNone(device["reference_model"])
            self.assertEqual(device["rated_power_kw"], 100)
            self.assertEqual(device["profile"], "generic-pv-100kw")
            self.assertEqual(device["scope"], "business")

    def test_codes_and_names_are_unique(self):
        codes = [device["device_code"] for device in self.devices]
        names = [device["name"] for device in self.business]
        self.assertEqual(len(set(codes)), 143)
        self.assertEqual(len(set(names)), 140)
        for device in self.devices:
            self.assertRegex(device["device_code"], r"^[A-Za-z0-9_-]{1,64}$")

    def test_boundary_codes(self):
        codes = {device["device_code"] for device in self.devices}
        for code in (
            "PV-01-INV-01",
            "PV-01-INV-48",
            "PV-02-INV-01",
            "PV-02-INV-40",
            "PV-03-INV-01",
            "PV-03-INV-32",
            "PV-04-INV-01",
            "PV-04-INV-20",
            "INV-1001",
            "INV-1002",
            "INV-1003",
        ):
            self.assertIn(code, codes)
        for code in ("PV-01-INV-49", "PV-02-INV-41", "PV-04-INV-21", "PV-01-INV-00"):
            self.assertNotIn(code, codes)

    def test_group_assignment(self):
        groups = {device["device_code"]: device["group_name"] for device in self.business}
        self.assertEqual(groups["PV-01-INV-01"], "PV-01-01区")
        self.assertEqual(groups["PV-01-INV-08"], "PV-01-01区")
        self.assertEqual(groups["PV-01-INV-09"], "PV-01-02区")
        self.assertEqual(groups["PV-01-INV-48"], "PV-01-06区")
        self.assertEqual(groups["PV-02-INV-40"], "PV-02-05区")
        self.assertEqual(groups["PV-03-INV-32"], "PV-03-04区")
        self.assertEqual(groups["PV-04-INV-20"], "PV-04-03区")
        for station in self.fleet.business_stations:
            codes = [device["device_code"] for device in self.business if device["station_code"] == station.station_code]
            for position, code in enumerate(codes, start=1):
                expected = f"{station.station_code}-{(position - 1) // station.group_size + 1:02d}区"
                self.assertEqual(groups[code], expected)

    def test_baseline_rows_keep_existing_identity(self):
        self.assertEqual([device["device_code"] for device in self.baseline], ["INV-1001", "INV-1002", "INV-1003"])
        for device in self.baseline:
            self.assertEqual(device["station_code"], "ST-01")
            self.assertEqual(device["scope"], "baseline")
            self.assertNotIn("vendor", device)
            self.assertNotIn("is_simulated", device)
            self.assertNotIn("reference_vendor", device)
            self.assertNotIn("reference_model", device)
            self.assertNotIn("profile", device)
            self.assertNotEqual(device.get("vendor"), "synthetic-fleet")

    def test_expansion_is_deterministic(self):
        self.assertEqual(expand_devices(load_fleet()), self.devices)


class FleetValidationTests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads(DEFAULT_FLEET_PATH.read_text(encoding="utf-8"))

    def assertRejected(self, data):
        with self.assertRaises(FleetError):
            parse_fleet(data)

    def test_rejects_unknown_top_level_field(self):
        data = copy.deepcopy(self.base)
        data["unexpected"] = True
        self.assertRejected(data)

    def test_rejects_missing_top_level_field(self):
        data = copy.deepcopy(self.base)
        del data["sampling_seconds"]
        self.assertRejected(data)

    def test_rejects_wrong_schema_version(self):
        for version in (2, "1", 1.0, True):
            data = copy.deepcopy(self.base)
            data["schema_version"] = version
            self.assertRejected(data)

    def test_rejects_non_positive_sampling_seconds(self):
        for value in (0, -5, True, 5.0):
            data = copy.deepcopy(self.base)
            data["sampling_seconds"] = value
            self.assertRejected(data)

    def test_rejects_unknown_station_field(self):
        data = copy.deepcopy(self.base)
        data["stations"][0]["owner"] = "ops"
        self.assertRejected(data)

    def test_rejects_duplicate_station_code(self):
        data = copy.deepcopy(self.base)
        data["stations"][1]["station_code"] = "PV-01"
        self.assertRejected(data)

    def test_rejects_duplicate_device_code(self):
        data = copy.deepcopy(self.base)
        data["stations"][-1]["explicit"].append("PV-01-INV-01")
        self.assertRejected(data)

    def test_rejects_duplicate_explicit_code(self):
        data = copy.deepcopy(self.base)
        data["stations"][-1]["explicit"].append("INV-1001")
        self.assertRejected(data)

    def test_rejects_invalid_scope(self):
        data = copy.deepcopy(self.base)
        data["stations"][0]["scope"] = "demo"
        self.assertRejected(data)

    def test_rejects_invalid_kind(self):
        data = copy.deepcopy(self.base)
        data["stations"][0]["kind"] = "pcs"
        self.assertRejected(data)

    def test_rejects_invalid_daylight_mode(self):
        for mode in ("demo", "REAL", ""):
            data = copy.deepcopy(self.base)
            data["stations"][0]["daylight_mode"] = mode
            self.assertRejected(data)

    def test_rejects_non_positive_device_count(self):
        for value in (0, -1, True, 4.8, "48"):
            data = copy.deepcopy(self.base)
            data["stations"][0]["device_count"] = value
            self.assertRejected(data)

    def test_rejects_non_positive_power_and_group_size(self):
        for field in ("rated_power_kw", "group_size"):
            for value in (0, -100, False, float("nan"), float("inf")):
                data = copy.deepcopy(self.base)
                data["stations"][0][field] = value
                self.assertRejected(data)

    def test_rejects_loop_without_minutes(self):
        data = copy.deepcopy(self.base)
        del data["stations"][-1]["daylight_loop_minutes"]
        self.assertRejected(data)

    def test_rejects_invalid_loop_minutes(self):
        for value in (0, -60, None, float("nan")):
            data = copy.deepcopy(self.base)
            data["stations"][-1]["daylight_loop_minutes"] = value
            self.assertRejected(data)

    def test_rejects_loop_minutes_with_real_mode(self):
        data = copy.deepcopy(self.base)
        data["stations"][0]["daylight_loop_minutes"] = 60
        self.assertRejected(data)

    def test_rejects_invalid_codes(self):
        for mutate in (
            lambda data: data["stations"][0].update({"station_code": "PV 01"}),
            lambda data: data["stations"][0].update({"device_prefix": "IN V"}),
            lambda data: data["stations"][-1].update({"explicit": ["INV-1001", "INV 1002"]}),
            lambda data: data["stations"][0].update({"station_code": "PV-01!"}),
            lambda data: data["stations"][-1].update({"explicit": ["INV-1001", ""]}),
        ):
            data = copy.deepcopy(self.base)
            mutate(data)
            self.assertRejected(data)

    def test_rejects_business_station_with_explicit(self):
        data = copy.deepcopy(self.base)
        data["stations"][0]["explicit"] = ["PV-01-INV-01"]
        self.assertRejected(data)

    def test_rejects_baseline_station_with_business_fields(self):
        data = copy.deepcopy(self.base)
        data["stations"][-1]["device_count"] = 3
        self.assertRejected(data)

    def test_rejects_empty_station_list(self):
        data = copy.deepcopy(self.base)
        data["stations"] = []
        self.assertRejected(data)

    def test_rejects_non_mapping_station(self):
        data = copy.deepcopy(self.base)
        data["stations"][0] = "PV-01"
        self.assertRejected(data)


if __name__ == "__main__":
    unittest.main()
