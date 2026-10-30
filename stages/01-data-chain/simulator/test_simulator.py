import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fleet import load_fleet
from simulator import (
    BASELINE_PROFILE,
    BASELINE_RATED_POWER_KW,
    CHINA_TZ,
    DEFAULT_FLUSH_LIMIT,
    DEVICES,
    WINDOW_MINUTES,
    WINDOW_SCENARIOS,
    DeviceConfig,
    Ledger,
    MidRegistry,
    SharedPublisher,
    build_device_configs,
    daylight_profile_ms,
    make_sample,
    simulation_day_index,
    window_active,
    window_device_code,
)

TEST_DB_DIR = Path(__file__).resolve().parents[3] / ".cache" / "simulator-tests"


def _test_db_path(name):
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    path = TEST_DB_DIR / f"{name}-{os.getpid()}.sqlite"
    _remove_sqlite_files(path)
    return path


def _remove_sqlite_files(path):
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


class DaylightLoopTests(unittest.TestCase):
    def setUp(self):
        self.night_ms = int(datetime(2026, 10, 15, 20, 30, tzinfo=CHINA_TZ).timestamp() * 1000)

    def test_cycle_restarts_daylight_with_real_timestamps_and_monotonic_sequences(self):
        for device in DEVICES:
            readings = []
            for index, elapsed in enumerate([0, 900, 1800, 2700, 3595, 3600, 3605, 7200]):
                timestamp = self.night_ms + elapsed * 1000
                profile = daylight_profile_ms(timestamp, elapsed, 60)
                raw, _ = make_sample(device, 293631 + index, timestamp, 2026, "stage4-live", profile_ts_ms=profile)
                sample = json.loads(raw)
                self.assertEqual(sample["ts_ms"], timestamp)
                self.assertEqual(sample["seq"], 293631 + index)
                self.assertEqual(sample["run_id"], "stage4-live")
                self.assertGreater(sample["power"], 20)
                self.assertLessEqual(sample["power"], 100)
                self.assertEqual(sample["status"], 1)
                calculated = math.sqrt(3) * sample["voltage"] * sample["current"] * 0.99 / 1000
                self.assertAlmostEqual(sample["power"], calculated, delta=0.002)
                readings.append(sample["power"])
            self.assertGreater(readings[2], readings[0])
        self.assertEqual(daylight_profile_ms(self.night_ms, 0, 60), daylight_profile_ms(self.night_ms, 3600, 60))

    def test_default_mode_still_uses_real_day_night_cycle(self):
        for device in DEVICES:
            raw, _ = make_sample(device, 1, self.night_ms, 2026, "normal")
            sample = json.loads(raw)
            self.assertEqual(sample["power"], 0)
            self.assertEqual(sample["current"], 0)
            self.assertEqual(sample["status"], 0)

    def test_rejects_invalid_cycle_lengths(self):
        for minutes in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                daylight_profile_ms(self.night_ms, 0, minutes)

    def test_retired_fault_injection_never_marks_a_sample(self):
        for device in DEVICES:
            for hour in (9, 13, 16, 22):
                stamp = int(datetime(2026, 10, 15, hour, 5, tzinfo=CHINA_TZ).timestamp() * 1000)
                raw, active = make_sample(device, 10, stamp, 2026, 'natural')
                sample = json.loads(raw)
                self.assertFalse(active)
                self.assertEqual(sample['fault_code'], 0)

    def test_fault_mode_is_inert_but_still_validated(self):
        stamp = int(datetime(2026, 10, 15, 12, 0, tzinfo=CHINA_TZ).timestamp() * 1000)
        for device in DEVICES:
            self.assertEqual(make_sample(device, 10, stamp, 2026, 'inert'),
                             make_sample(device, 10, stamp, 2026, 'inert', fault_mode='off'))
        for mode in ('profile', '', None, 'OFF'):
            with self.assertRaises(ValueError):
                make_sample(DEVICES[0], 1, stamp, 2026, 'invalid', fault_mode=mode)

    def test_invalid_window_mode_rejected(self):
        stamp = int(datetime(2026, 10, 15, 12, 0, tzinfo=CHINA_TZ).timestamp() * 1000)
        for mode in ('ON', 'true', '', None):
            with self.assertRaises(ValueError):
                make_sample(DEVICES[0], 1, stamp, 2026, 'invalid', window_mode=mode)

    def test_mode_change_retains_ledger_queue(self):
        path = _test_db_path("mode-change")
        ledger = None
        resumed = None
        try:
            device = DEVICES[0]
            stamp = int(datetime(2026, 10, 15, 12, 0, tzinfo=CHINA_TZ).timestamp() * 1000)
            ledger = Ledger(path)
            raw, active = make_sample(device, 0, stamp, 2026, 'same-run')
            ledger.add(device, 0, stamp, raw, active)
            ledger.db.close()
            resumed = Ledger(path)
            self.assertEqual(resumed.next_seq(device), 1)
            self.assertEqual(resumed.counts(), (1, 1))
            new_raw, new_active = make_sample(device, 1, stamp + 5000, 2026, 'same-run', window_mode='off')
            resumed.add(device, 1, stamp + 5000, new_raw, new_active)
            self.assertEqual(resumed.db.execute('SELECT fault_active FROM generated ORDER BY seq').fetchall(),
                             [(0,), (0,)])
            resumed.acknowledge(device, 0)
            self.assertEqual(resumed.counts(), (2, 1))
            self.assertEqual(resumed.pending(device)[0][0], 1)
        finally:
            for handle in (resumed, ledger):
                if handle is not None:
                    handle.db.close()
            _remove_sqlite_files(path)


class FleetDeviceConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configs = build_device_configs()
        cls.by_code = {config.device_code: config for config in cls.configs}
        cls.noon_ms = int(datetime(2026, 10, 15, 12, 0, tzinfo=CHINA_TZ).timestamp() * 1000)
        cls.night_ms = int(datetime(2026, 10, 15, 22, 0, tzinfo=CHINA_TZ).timestamp() * 1000)

    def sample(self, config, *, seq=100, ts_ms=None, fault_mode="scheduled", window_mode="on", profile_ts_ms=None):
        raw, active = make_sample(
            config.device_code,
            seq,
            self.noon_ms if ts_ms is None else ts_ms,
            2026,
            "fleet-test",
            profile_ts_ms=profile_ts_ms,
            fault_mode=fault_mode,
            window_mode=window_mode,
            config=config,
        )
        return json.loads(raw), active

    def test_builds_143_frozen_runtime_configs(self):
        self.assertEqual(len(self.configs), 143)
        self.assertEqual(len(self.by_code), 143)
        counts = {}
        for config in self.configs:
            counts[config.station_code] = counts.get(config.station_code, 0) + 1
        self.assertEqual(counts, {"PV-01": 48, "PV-02": 40, "PV-03": 32, "PV-04": 20, "ST-01": 3})
        self.assertEqual(len([c for c in self.configs if c.scope == "business"]), 140)
        self.assertEqual(len([c for c in self.configs if c.scope == "baseline"]), 3)
        with self.assertRaises(FrozenInstanceError):
            self.configs[0].derate_factor = 0.5

    def test_config_carries_no_ledger_write_fields(self):
        names = {field.name for field in fields(DeviceConfig)}
        for name in ("name", "dev_type", "vendor", "is_simulated", "reference_vendor", "reference_model"):
            self.assertNotIn(name, names)

    def test_station_and_device_code_mapping(self):
        first = self.by_code["PV-01-INV-01"]
        self.assertEqual(first.station_code, "PV-01")
        self.assertEqual(first.station_name, "示范园区 A 屋顶光伏片区")
        self.assertEqual(first.group_name, "PV-01-01区")
        self.assertEqual(first.rated_power_kw, 100)
        self.assertEqual(first.profile, "generic-pv-100kw")
        self.assertEqual(first.daylight_mode, "real")
        self.assertIsNone(first.daylight_loop_minutes)
        self.assertEqual(first.scope, "business")
        self.assertEqual(self.by_code["PV-01-INV-09"].group_name, "PV-01-02区")
        self.assertEqual(self.by_code["PV-02-INV-40"].group_name, "PV-02-05区")
        self.assertEqual(self.by_code["PV-04-INV-20"].group_name, "PV-04-03区")

        for code in DEVICES:
            baseline = self.by_code[code]
            self.assertEqual(baseline.station_code, "ST-01")
            self.assertEqual(baseline.scope, "baseline")
            self.assertEqual(baseline.daylight_mode, "loop")
            self.assertEqual(baseline.daylight_loop_minutes, 60)
            self.assertEqual(baseline.rated_power_kw, BASELINE_RATED_POWER_KW)
            self.assertEqual(baseline.profile, BASELINE_PROFILE)
            self.assertIsNone(baseline.group_name)

        for code in ("PV-01-INV-01", "PV-02-INV-40", "PV-03-INV-32", "PV-04-INV-20", "INV-1002"):
            payload, _ = self.sample(self.by_code[code])
            self.assertEqual(payload["station_id"], self.by_code[code].station_code)
            self.assertEqual(payload["device_id"], code)

    def test_same_station_shares_weather_with_small_individual_differences(self):
        station = [config for config in self.configs if config.station_code == "PV-01"]
        self.assertEqual(len(station), 48)
        self.assertEqual(len({config.station_cloud_base for config in station}), 1)
        self.assertEqual(len({config.station_phase for config in station}), 1)
        self.assertEqual(len({config.derate_factor for config in station}), len(station))

        readings = [self.sample(config)[0] for config in station]
        powers = [reading["power"] for reading in readings]
        self.assertGreater(max(powers) - min(powers), 0.5)
        self.assertLess(max(powers) - min(powers), 6.0)
        normalized = [reading["power"] / config.derate_factor for reading, config in zip(readings, station)]
        self.assertLess(max(normalized) / min(normalized), 1.02)
        self.assertGreater(len({round(reading["temperature"], 3) for reading in readings}), 40)

    def test_stations_have_stable_distinct_weather(self):
        codes = ("PV-01", "PV-02", "PV-03", "PV-04")
        bases = {code: next(c.station_cloud_base for c in self.configs if c.station_code == code) for code in codes}
        phases = {code: next(c.station_phase for c in self.configs if c.station_code == code) for code in codes}
        self.assertEqual(len(set(bases.values())), 4)
        self.assertEqual(len(set(phases.values())), 4)
        for seq in (100, 500):
            means = {}
            for code in codes:
                powers = [self.sample(c, seq=seq)[0]["power"] for c in self.configs if c.station_code == code]
                means[code] = sum(powers) / len(powers)
            self.assertGreater(max(means.values()) - min(means.values()), 0.8)

    def test_config_order_does_not_change_device_characteristics(self):
        fleet = load_fleet()
        reordered = build_device_configs(replace(fleet, stations=tuple(reversed(fleet.stations))))
        self.assertEqual({config.device_code: config for config in reordered}, self.by_code)
        self.assertEqual(build_device_configs(), self.configs)
        self.assertEqual(build_device_configs(load_fleet()), self.configs)

    def test_process_restart_reproduces_identical_samples(self):
        config = self.by_code["PV-02-INV-11"]
        expected = self.sample(config)
        script = (
            "import json;"
            "from simulator import build_device_configs, make_sample;"
            f"config = {{c.device_code: c for c in build_device_configs()}}[{config.device_code!r}];"
            f"raw, active = make_sample(config.device_code, 100, {self.noon_ms}, 2026, 'fleet-test', config=config);"
            "print(json.dumps([json.loads(raw), active], ensure_ascii=False))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONHASHSEED="12345"),
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload, active = json.loads(completed.stdout)
        self.assertEqual((payload, active), expected)

    def test_no_device_ever_reports_a_scheduled_fault(self):
        # Business and baseline devices both stay on the natural curve; only the
        # five windows may move a metric, and they never set fault_code/active.
        for config in self.configs:
            for hour in (7, 11, 15, 19):
                stamp = int(datetime(2026, 10, 15, hour, 30, tzinfo=CHINA_TZ).timestamp() * 1000)
                payload, active = self.sample(config, seq=10, ts_ms=stamp)
                self.assertFalse(active)
                self.assertEqual(payload["fault_code"], 0)

    def test_baseline_config_matches_legacy_sampling(self):
        for device in DEVICES:
            config = self.by_code[device]
            stamp = int(datetime(2026, 10, 15, 12, 0, tzinfo=CHINA_TZ).timestamp() * 1000)
            fleet_raw, fleet_active = self.sample(config, seq=10, ts_ms=stamp, profile_ts_ms=self.night_ms)
            legacy_raw, legacy_active = make_sample(device, 10, stamp, 2026, "fleet-test", profile_ts_ms=self.night_ms)
            self.assertEqual(fleet_raw, json.loads(legacy_raw))
            self.assertEqual(fleet_active, legacy_active)
            self.assertEqual(json.loads(legacy_raw)["station_id"], "ST-01")

    def test_daylight_mode_is_metadata_and_profile_timestamp_stays_caller_controlled(self):
        config = self.by_code["PV-01-INV-01"]
        self.assertEqual(config.daylight_mode, "real")
        lit, _ = self.sample(config, ts_ms=self.night_ms, profile_ts_ms=self.noon_ms)
        self.assertEqual(lit["ts_ms"], self.night_ms)
        self.assertGreater(lit["power"], 20)
        dark, _ = self.sample(config, ts_ms=self.night_ms)
        self.assertEqual(dark["power"], 0)
        self.assertEqual(dark["current"], 0)

    def test_runtime_config_must_match_device(self):
        with self.assertRaises(ValueError):
            make_sample("PV-01-INV-02", 1, self.noon_ms, 2026, "mismatch", config=self.by_code["PV-01-INV-01"])

    def test_baseline_config_preserves_legacy_sample(self):
        config = self.by_code["INV-1001"]
        configured = make_sample("INV-1001", 7, self.noon_ms, 2026, "compat", config=config)
        legacy = make_sample("INV-1001", 7, self.noon_ms, 2026, "compat")
        self.assertEqual(configured, legacy)

    def test_power_cap_and_electrical_consistency(self):
        for config in self.configs:
            for hour, minute in ((6, 30), (12, 0), (17, 30), (22, 0)):
                stamp = datetime(2026, 10, 15, hour, minute, tzinfo=CHINA_TZ)
                payload, _ = self.sample(
                    config, seq=hour * 12 + minute // 5, ts_ms=int(stamp.timestamp() * 1000)
                )
                self.assertGreaterEqual(payload["power"], 0)
                self.assertLessEqual(payload["power"], config.rated_power_kw)
                self.assertGreaterEqual(payload["voltage"], 320)
                self.assertLessEqual(payload["voltage"], 460)
                self.assertGreaterEqual(payload["current"], 0)
                self.assertLessEqual(payload["current"], 160.4)
                calculated = math.sqrt(3) * payload["voltage"] * payload["current"] * 0.99 / 1000
                self.assertAlmostEqual(payload["power"], calculated, delta=0.002)
                self.assertEqual(payload["status"], 1 if payload["power"] > 0.05 else 0)


# Backend natural-alarm thresholds these windows are expected to exercise.
# Power uses the high-limit rule, not the retired ``power < 20`` rule that
# would misfire every night.
STATION_RULE_LIMITS = {
    "PV-01": {"temperature": 55.0, "voltage": 430.0, "current": 180.0, "power": 110.0},
    "PV-02": {"temperature": 75.0, "voltage": 430.0, "current": 180.0, "power": 110.0},
    "PV-03": {"temperature": 80.0, "voltage": 430.0, "current": 180.0, "power": 110.0},
    "PV-04": {"temperature": 55.0, "voltage": 430.0, "current": 180.0, "power": 110.0},
    "ST-01": {"temperature": 80.0, "voltage": 430.0, "current": 180.0, "power": 110.0},
}


class WindowInjectionTests(unittest.TestCase):
    DAY = date(2026, 10, 21)

    @classmethod
    def setUpClass(cls):
        cls.configs = build_device_configs()
        cls.by_code = {config.device_code: config for config in cls.configs}
        cls.by_station = {}
        for config in cls.configs:
            cls.by_station.setdefault(config.station_code, []).append(config)
        cls.station_codes = {
            station: sorted(config.device_code for config in configs)
            for station, configs in cls.by_station.items()
        }

    def stamp(self, hour, *, minute=0, second=0, day=None):
        day = self.DAY if day is None else day
        return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=CHINA_TZ)

    def sample(self, config, instant, *, seq=100, fault_mode="scheduled", window_mode="on"):
        raw, active = make_sample(
            config.device_code,
            seq,
            int(instant.timestamp() * 1000),
            2026,
            "window-test",
            fault_mode=fault_mode,
            window_mode=window_mode,
            config=config,
        )
        return json.loads(raw), active

    @staticmethod
    def expected_index(day, count):
        # Shanghai midnight is the previous UTC day, which is exactly what
        # Go's SimulationDay(day).Unix()/86400 returns.
        return (day.toordinal() - date(1970, 1, 2).toordinal()) % count

    def target(self, station, day=None):
        codes = self.station_codes[station]
        return codes[self.expected_index(day or self.DAY, len(codes))]

    def test_day_index_matches_backend_epoch_day_rule(self):
        for day in (date(2026, 10, 21), date(2026, 10, 22), date(2024, 2, 29), date(2026, 1, 1)):
            for count in (48, 40, 32, 20, 3):
                self.assertEqual(simulation_day_index(day, count), self.expected_index(day, count))
        self.assertEqual(simulation_day_index(self.DAY, 0), 0)

    def test_window_is_ten_minutes_on_the_real_beijing_clock(self):
        self.assertEqual(WINDOW_MINUTES, 10)
        for scenario in WINDOW_SCENARIOS:
            starts = self.stamp(scenario.hour)
            self.assertTrue(window_active(starts, scenario))
            self.assertTrue(window_active(starts + timedelta(minutes=9, seconds=59), scenario))
            self.assertFalse(window_active(starts - timedelta(seconds=1), scenario))
            self.assertFalse(window_active(starts + timedelta(minutes=10), scenario))
            other = next(item for item in WINDOW_SCENARIOS if item is not scenario)
            self.assertFalse(window_active(starts, other))

    def test_selection_matches_backend_sorted_code_rotation(self):
        for scenario in WINDOW_SCENARIOS:
            codes = self.station_codes[scenario.station_code]
            expected = codes[self.expected_index(self.DAY, len(codes))]
            # Manifest/list order must not matter.
            self.assertEqual(window_device_code(scenario, tuple(reversed(codes)), self.DAY), expected)
            self.assertEqual(self.target(scenario.station_code), expected)
            tomorrow = self.DAY + timedelta(days=1)
            self.assertEqual(
                window_device_code(scenario, codes, tomorrow),
                codes[(self.expected_index(self.DAY, len(codes)) + 1) % len(codes)],
            )

    def test_rebuild_and_reordered_manifest_pick_the_same_device(self):
        fleet = load_fleet()
        rebuilt = build_device_configs()
        reordered = build_device_configs(replace(fleet, stations=tuple(reversed(fleet.stations))))
        for scenario in WINDOW_SCENARIOS:
            first = {config.device_code: config for config in rebuilt}
            second = {config.device_code: config for config in reordered}
            payload_a, _ = self.sample(first[self.target(scenario.station_code)], self.stamp(scenario.hour, minute=5))
            payload_b, _ = self.sample(second[self.target(scenario.station_code)], self.stamp(scenario.hour, minute=5))
            self.assertEqual(payload_a, payload_b)

    def test_only_selected_device_and_metric_crosses_in_window(self):
        for scenario in WINDOW_SCENARIOS:
            limits = STATION_RULE_LIMITS[scenario.station_code]
            instant = self.stamp(scenario.hour, minute=5)
            target = self.target(scenario.station_code)
            hits = []
            for config in self.by_station[scenario.station_code]:
                payload, active = self.sample(config, instant)
                self.assertFalse(active)
                self.assertEqual(payload["fault_code"], 0)
                if payload[scenario.metric] > limits[scenario.metric]:
                    hits.append(config.device_code)
                for metric, limit in limits.items():
                    if config.device_code == target and metric == scenario.metric:
                        continue
                    self.assertLessEqual(
                        payload[metric],
                        limit,
                        f"{scenario.station_code}/{config.device_code} {metric} crossed outside its window",
                    )
            self.assertEqual(hits, [target])
            payload, _ = self.sample(self.by_code[target], instant)
            self.assertGreaterEqual(payload[scenario.metric], scenario.target)

    def test_window_end_returns_to_the_natural_value(self):
        for scenario in WINDOW_SCENARIOS:
            limits = STATION_RULE_LIMITS[scenario.station_code]
            config = self.by_code[self.target(scenario.station_code)]
            inside, _ = self.sample(config, self.stamp(scenario.hour))
            after, _ = self.sample(config, self.stamp(scenario.hour, minute=WINDOW_MINUTES))
            disabled, _ = self.sample(config, self.stamp(scenario.hour, minute=WINDOW_MINUTES), window_mode="off")
            self.assertGreater(inside[scenario.metric], limits[scenario.metric])
            self.assertLessEqual(after[scenario.metric], limits[scenario.metric])
            self.assertEqual(after, disabled)

    def test_window_mode_off_disables_the_window(self):
        for scenario in WINDOW_SCENARIOS:
            limits = STATION_RULE_LIMITS[scenario.station_code]
            config = self.by_code[self.target(scenario.station_code)]
            for minute in (0, 5, 9):
                payload, active = self.sample(config, self.stamp(scenario.hour, minute=minute), window_mode="off")
                self.assertFalse(active)
                self.assertLessEqual(payload[scenario.metric], limits[scenario.metric])

    def test_default_samples_obey_ingress_contract_at_every_window(self):
        for scenario in WINDOW_SCENARIOS:
            instant = self.stamp(scenario.hour, minute=5)
            for config in self.configs:
                raw, _ = make_sample(config.device_code, 100, int(instant.timestamp() * 1000),
                                     2026, "safe-default", fault_mode="off", config=config)
                payload = json.loads(raw)
                self.assertLessEqual(payload["temperature"], 85)
                self.assertLessEqual(payload["current"], 160.4)
                self.assertLessEqual(payload["power"], 110)
                calculated = math.sqrt(3) * payload["voltage"] * payload["current"] * .99 / 1000
                self.assertAlmostEqual(payload["power"], calculated, delta=2)

    def test_fault_mode_does_not_gate_the_window(self):
        # Production runs with --fault-mode off; windows must still inject.
        for scenario in WINDOW_SCENARIOS:
            config = self.by_code[self.target(scenario.station_code)]
            for minute in (0, 5, 9):
                payload, active = self.sample(config, self.stamp(scenario.hour, minute=minute), fault_mode="off")
                self.assertFalse(active)
                self.assertGreaterEqual(payload[scenario.metric], scenario.target)

    def test_non_window_hours_stay_below_rules(self):
        for scenario in WINDOW_SCENARIOS:
            limits = STATION_RULE_LIMITS[scenario.station_code]
            for hour, minute in ((0, 0), (6, 30), (12, 0), (18, 30), (23, 55)):
                for config in self.by_station[scenario.station_code]:
                    payload, _ = self.sample(config, self.stamp(hour, minute=minute))
                    for metric, limit in limits.items():
                        self.assertLessEqual(payload[metric], limit)

    def test_temperature_injection_keeps_electrical_identity(self):
        for scenario in WINDOW_SCENARIOS:
            if scenario.metric != "temperature":
                continue
            config = self.by_code[self.target(scenario.station_code)]
            instant = self.stamp(scenario.hour, minute=5)
            payload, _ = self.sample(config, instant)
            natural, _ = self.sample(config, instant, window_mode="off")
            for metric in ("power", "voltage", "current", "status"):
                self.assertEqual(payload[metric], natural[metric])
            self.assertGreaterEqual(payload["temperature"], scenario.target)
            self.assertLessEqual(payload["temperature"], 85)

    def test_voltage_injection_recomputes_current(self):
        scenario = next(item for item in WINDOW_SCENARIOS if item.metric == "voltage")
        config = self.by_code[self.target(scenario.station_code)]
        payload, _ = self.sample(config, self.stamp(scenario.hour, minute=5))
        self.assertGreaterEqual(payload["voltage"], scenario.target)
        calculated = math.sqrt(3) * payload["voltage"] * payload["current"] * 0.99 / 1000
        self.assertAlmostEqual(payload["power"], calculated, delta=0.002)

    def test_baseline_temperature_injection_stays_inside_the_other_rules(self):
        scenario = next(item for item in WINDOW_SCENARIOS if item.station_code == "ST-01")
        config = self.by_code[self.target("ST-01")]
        payload, active = self.sample(config, self.stamp(scenario.hour, minute=5))
        self.assertFalse(active)
        self.assertGreaterEqual(payload["temperature"], scenario.target)
        self.assertEqual(payload["status"], 1)
        self.assertLessEqual(payload["current"], 160.4)
        self.assertLessEqual(payload["power"], 110)
        self.assertLessEqual(payload["temperature"], 85)
        calculated = math.sqrt(3) * payload["voltage"] * payload["current"] * 0.99 / 1000
        self.assertAlmostEqual(payload["power"], calculated, delta=0.002)

    def test_station_rules_only_trip_inside_their_window(self):
        limits = STATION_RULE_LIMITS["ST-01"]
        base = self.stamp(0, day=date(2026, 10, 15))
        codes = self.station_codes["ST-01"]
        for minute in range(0, 24 * 60, 5):
            instant = base + timedelta(minutes=minute)
            target = codes[self.expected_index(instant.date(), len(codes))]
            for config in self.by_station["ST-01"]:
                payload, _ = self.sample(config, instant, seq=minute)
                injecting = config.device_code == target and 17 * 60 <= minute < 17 * 60 + WINDOW_MINUTES
                for metric, limit in limits.items():
                    if injecting and metric == "temperature":
                        self.assertGreater(payload["temperature"], 80)
                    else:
                        self.assertLessEqual(payload[metric], limit, f"{config.device_code} {metric} at {instant}")


class FakePublishInfo:
    def __init__(self, mid, rc=0):
        self.mid = mid
        self.rc = rc


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeMqttClient:
    """Minimal paho-like client; no network, no paho import."""

    def __init__(self):
        self.published = []
        self.next_mid = 1
        self.rc = 0
        self.on_connect = None
        self.on_disconnect = None
        self.on_publish = None

    def publish(self, topic, payload, qos=1):
        info = FakePublishInfo(self.next_mid, self.rc)
        self.next_mid += 1
        self.published.append((topic, payload, qos, info.mid))
        return info


class ImmediateAckClient(FakeMqttClient):
    """Delivers the PUBACK synchronously, before publish() returns its mid."""

    def __init__(self, *, ack_mid=None):
        super().__init__()
        self.ack_mid = ack_mid

    def publish(self, topic, payload, qos=1):
        info = FakePublishInfo(self.next_mid, self.rc)
        self.next_mid += 1
        self.published.append((topic, payload, qos, info.mid))
        if self.on_publish is not None:
            self.on_publish(self, None, info.mid if self.ack_mid is None else self.ack_mid, 0, None)
        return info


class BlockingFakeClient(FakeMqttClient):
    """Blocks inside publish() so a concurrent callback can be observed."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def publish(self, topic, payload, qos=1):
        self.entered.set()
        self.release.wait(2.0)
        return super().publish(topic, payload, qos)


class SendLockFakeClient(FakeMqttClient):
    """Fake paho client whose PUBACK delivery holds the internal send lock.

    This mirrors paho 2.1.0: ``_handle_pubackcomp`` holds
    ``_out_message_mutex`` while it invokes ``on_publish``. A publisher that
    calls ``client.publish`` while holding a lock the callback needs deadlocks.
    """

    def __init__(self):
        super().__init__()
        self.out_message_mutex = threading.Lock()
        self.ack_queue = queue.Queue()
        self.callback_entered = threading.Event()
        self.release_callback = threading.Event()

    def publish(self, topic, payload, qos=1):
        with self.out_message_mutex:
            return super().publish(topic, payload, qos)

    def deliver(self, mid, *, hold=None):
        self.ack_queue.put((mid, hold))

    def stop(self):
        self.ack_queue.put(None)

    def network_loop(self):
        while True:
            item = self.ack_queue.get()
            if item is None:
                return
            mid, hold = item
            with self.out_message_mutex:
                if hold is not None:
                    self.callback_entered.set()
                    hold.wait(2.0)
                self.on_publish(self, None, mid, 0, None)


class MidRegistryTests(unittest.TestCase):
    def test_register_rejects_unconsumed_duplicate(self):
        registry = MidRegistry()
        self.assertTrue(registry.register(7, "PV-01-INV-01", 3))
        self.assertFalse(registry.register(7, "PV-02-INV-01", 9))
        self.assertEqual(registry.pop(7), ("PV-01-INV-01", 3))
        self.assertIsNone(registry.pop(7))
        self.assertTrue(registry.register(7, "PV-02-INV-01", 9))
        self.assertEqual(registry.pop(7), ("PV-02-INV-01", 9))

    def test_clear_returns_removed_count(self):
        registry = MidRegistry()
        for mid in (1, 2, 3):
            registry.register(mid, "PV-01-INV-01", mid)
        self.assertEqual(len(registry), 3)
        self.assertEqual(registry.clear(), 3)
        self.assertEqual(registry.clear(), 0)
        self.assertEqual(len(registry), 0)


class LedgerPendingAllTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.ledger.add("PV-02-INV-01", 0, 0, "b0", False)
        self.ledger.add("PV-01-INV-01", 1, 0, "a1", False)
        self.ledger.add("PV-01-INV-01", 0, 0, "a0", False)

    def tearDown(self):
        self.ledger.db.close()

    def test_orders_by_device_then_seq_without_deleting(self):
        expected = [("PV-01-INV-01", 0, "a0"), ("PV-01-INV-01", 1, "a1"), ("PV-02-INV-01", 0, "b0")]
        self.assertEqual(self.ledger.pending_all(10), expected)
        self.assertEqual(self.ledger.pending_all(10), expected)
        self.assertEqual(self.ledger.pending_all(1), expected[:1])
        self.assertEqual(self.ledger.counts(), (3, 3))

    def test_rejects_non_positive_or_non_integer_limit(self):
        for limit in (0, -1, True, 1.5, "3", None):
            with self.assertRaises(ValueError):
                self.ledger.pending_all(limit)


class SharedPublisherTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.client = FakeMqttClient()
        self.publisher = SharedPublisher(self.ledger, client=self.client, client_id="test-fleet")

    def tearDown(self):
        self.ledger.db.close()

    def add(self, device_code, seq):
        payload = f"{device_code}:{seq}"
        self.ledger.add(device_code, seq, seq, payload, False)
        return payload

    def ack(self, mid):
        self.client.on_publish(self.client, None, mid, 0, None)

    def test_two_devices_with_same_seq_do_not_cross(self):
        payload_a = self.add("PV-01-INV-01", 5)
        payload_b = self.add("PV-02-INV-01", 5)
        mid_a = self.publisher.publish("PV-01-INV-01", 5, payload_a)
        mid_b = self.publisher.publish("PV-02-INV-01", 5, payload_b)
        self.assertIsNotNone(mid_a)
        self.assertIsNotNone(mid_b)
        self.assertNotEqual(mid_a, mid_b)

        self.ack(mid_b)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(5, payload_a)])
        self.assertEqual(self.ledger.pending("PV-02-INV-01"), [])

    def test_out_of_order_acknowledgement_only_removes_matching_rows(self):
        payloads = [self.add("PV-01-INV-01", seq) for seq in range(3)]
        mids = [self.publisher.publish("PV-01-INV-01", seq, payloads[seq]) for seq in range(3)]

        self.ack(mids[2])
        self.ack(mids[0])
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(1, payloads[1])])

    def test_unknown_mid_does_not_delete_queue(self):
        payload = self.add("PV-01-INV-01", 0)
        mid = self.publisher.publish("PV-01-INV-01", 0, payload)
        self.ack(9999)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(0, payload)])
        self.assertEqual(len(self.publisher.registry), 1)
        self.ack(mid)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [])

    def test_duplicate_mid_is_rejected_without_overwriting_owner(self):
        payload = self.add("PV-01-INV-01", 0)
        occupied_mid = self.client.next_mid
        self.publisher.registry.register(occupied_mid, "PV-09-INV-01", 99)

        self.assertIsNone(self.publisher.publish("PV-01-INV-01", 0, payload))
        self.assertEqual(self.publisher.registry.pop(occupied_mid), ("PV-09-INV-01", 99))
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(0, payload)])

    def test_disconnect_clears_registry_but_keeps_outbox(self):
        payload = self.add("PV-01-INV-01", 0)
        mid = self.publisher.publish("PV-01-INV-01", 0, payload)
        self.assertEqual(len(self.publisher.registry), 1)

        self.client.on_disconnect(self.client, None, None, "keepalive timeout", None)
        self.assertEqual(len(self.publisher.registry), 0)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(0, payload)])

        self.ack(mid)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(0, payload)])

    def test_reconnect_flush_is_bounded_and_can_continue(self):
        payloads = [self.add("PV-01-INV-01", seq) for seq in range(5)]
        bounded = SharedPublisher(self.ledger, client=self.client, reconnect_flush_limit=3)

        bounded.on_connect(self.client, None, {}, "Success", None)
        # on_connect only asks a worker to flush, so wait for that batch.
        self.assertTrue(_wait_until(lambda: len(self.client.published) == 3))
        self.assertEqual(self.ledger.counts()[1], 5)

        for _topic, _payload, _qos, mid in list(self.client.published):
            self.ack(mid)
        self.assertEqual(self.ledger.counts()[1], 2)

        self.assertEqual(bounded.flush_pending(10), 2)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(3, payloads[3]), (4, payloads[4])])

    def test_default_flush_limit_is_bounded(self):
        self.assertLessEqual(DEFAULT_FLUSH_LIMIT, 1000)
        for limit in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                SharedPublisher(self.ledger, client=self.client, reconnect_flush_limit=limit)

    def test_publish_failure_does_not_register_mid(self):
        payload = self.add("PV-01-INV-01", 0)
        self.client.rc = 4
        self.assertIsNone(self.publisher.publish("PV-01-INV-01", 0, payload))
        self.assertEqual(len(self.publisher.registry), 0)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(0, payload)])

        self.client.rc = 0
        self.assertIsNotNone(self.publisher.publish("PV-01-INV-01", 0, payload))

    def test_fast_puback_before_registration_acks_the_published_row(self):
        client = ImmediateAckClient()
        publisher = SharedPublisher(self.ledger, client=client, client_id="fast-ack")
        payload = self.add("PV-01-INV-01", 7)
        other = self.add("PV-02-INV-01", 7)

        mid = publisher.publish("PV-01-INV-01", 7, payload)
        self.assertIsNotNone(mid)
        self.assertEqual(len(publisher.registry), 0)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [])
        self.assertEqual(self.ledger.pending("PV-02-INV-01"), [(7, other)])

    def test_unknown_mid_during_publish_is_discarded_when_it_does_not_match(self):
        client = ImmediateAckClient(ack_mid=999)
        publisher = SharedPublisher(self.ledger, client=client, client_id="early-mismatch")
        payload = self.add("PV-01-INV-01", 0)

        mid = publisher.publish("PV-01-INV-01", 0, payload)
        self.assertIsNotNone(mid)
        self.assertEqual(len(publisher.registry), 1)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(0, payload)])

        client.on_publish(client, None, 999, 0, None)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(0, payload)])
        client.on_publish(client, None, mid, 0, None)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [])

    def test_failed_publish_with_staged_mid_does_not_ack(self):
        client = ImmediateAckClient()
        client.rc = 4
        publisher = SharedPublisher(self.ledger, client=client, client_id="early-failed")
        payload = self.add("PV-01-INV-01", 0)

        self.assertIsNone(publisher.publish("PV-01-INV-01", 0, payload))
        self.assertEqual(len(publisher.registry), 0)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(0, payload)])

    def test_disconnect_inside_publish_keeps_outbox_and_does_not_register_stale_mid(self):
        client = FakeMqttClient()
        publisher = SharedPublisher(self.ledger, client=client, client_id="disconnect-in-publish")
        payload = self.add("PV-01-INV-01", 0)
        original_publish = client.publish

        def disconnected_publish(*args, **kwargs):
            result = original_publish(*args, **kwargs)
            publisher.on_disconnect(client, None, None, "disconnect", None)
            return result

        client.publish = disconnected_publish
        self.assertIsNone(publisher.publish("PV-01-INV-01", 0, payload))
        self.assertEqual(len(publisher.registry), 0)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [(0, payload)])

    def test_reconnect_rebinds_mids_without_crossing_devices(self):
        payload_a = self.add("PV-01-INV-01", 0)
        payload_b = self.add("PV-02-INV-01", 0)
        old_a = self.publisher.publish("PV-01-INV-01", 0, payload_a)
        old_b = self.publisher.publish("PV-02-INV-01", 0, payload_b)

        self.client.on_disconnect(self.client, None, None, "keepalive timeout", None)
        self.publisher.on_connect(self.client, None, {}, "Success", None)
        self.assertTrue(_wait_until(lambda: len(self.client.published) == 4))
        new_mids = [mid for _topic, _payload, _qos, mid in self.client.published[2:]]

        # Stale mids from the previous session must not acknowledge anything.
        self.client.on_publish(self.client, None, old_a, 0, None)
        self.client.on_publish(self.client, None, old_b, 0, None)
        self.assertEqual(self.ledger.counts()[1], 2)

        self.client.on_publish(self.client, None, new_mids[0], 0, None)
        self.assertEqual(self.ledger.pending("PV-01-INV-01"), [])
        self.assertEqual(self.ledger.pending("PV-02-INV-01"), [(0, payload_b)])
        self.client.on_publish(self.client, None, new_mids[1], 0, None)
        self.assertEqual(self.ledger.counts()[1], 0)

    def test_on_connect_does_not_block_on_the_publish_lock(self):
        ledger = Ledger(":memory:")
        client = BlockingFakeClient()
        publisher = SharedPublisher(ledger, client=client, client_id="connect-async")
        ledger.add("PV-01-INV-01", 0, 0, "a", False)
        ledger.add("PV-02-INV-01", 0, 0, "b", False)
        publish_thread = threading.Thread(
            target=publisher.publish, args=("PV-01-INV-01", 0, "a"), daemon=True
        )
        publish_thread.start()
        try:
            self.assertTrue(client.entered.wait(2.0))
            started = time.monotonic()
            publisher.on_connect(client, None, {}, "Success", None)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(len(client.published), 0)
        finally:
            client.release.set()
            publish_thread.join(2.0)
        self.assertFalse(publish_thread.is_alive())
        self.assertTrue(_wait_until(lambda: len(client.published) == 3))
        self.assertTrue(_wait_until(lambda: publisher._flush_worker is None))
        ledger.db.close()

    def test_publish_does_not_deadlock_with_puback_send_lock(self):
        ledger = Ledger(":memory:")
        client = SendLockFakeClient()
        publisher = SharedPublisher(ledger, client=client, client_id="lock-order")
        ledger.add("PV-01-INV-01", 0, 0, "a", False)
        ledger.add("PV-02-INV-01", 0, 0, "b", False)
        network = threading.Thread(target=client.network_loop, daemon=True)
        network.start()
        results, errors = [], []
        publisher_thread = None
        try:
            mid_a = publisher.publish("PV-01-INV-01", 0, "a")
            self.assertIsNotNone(mid_a)

            # The callback holds the client's send lock while invoking on_publish.
            client.deliver(mid_a, hold=client.release_callback)
            self.assertTrue(client.callback_entered.wait(2.0))

            def publish_second():
                try:
                    results.append(publisher.publish("PV-02-INV-01", 0, "b"))
                except Exception as exc:  # pragma: no cover - surfaced by assertions
                    errors.append(exc)

            publisher_thread = threading.Thread(target=publish_second, daemon=True)
            publisher_thread.start()
            time.sleep(0.05)
            client.release_callback.set()
            publisher_thread.join(2.0)
            self.assertFalse(publisher_thread.is_alive(), "publish must not block on the PUBACK send lock")
            self.assertEqual(errors, [])
            self.assertIsNotNone(results[0])
            self.assertEqual(ledger.pending("PV-01-INV-01"), [], "callback must not block on the publisher lock")
            self.assertEqual(ledger.pending("PV-02-INV-01"), [(0, "b")])

            client.deliver(results[0])
            self.assertTrue(_wait_until(lambda: ledger.pending("PV-02-INV-01") == []))
        finally:
            client.stop()
            network.join(2.0)
            if publisher_thread is not None:
                publisher_thread.join(2.0)
            ledger.db.close()

    def test_concurrent_register_and_pop_keeps_ownership(self):
        registry = MidRegistry()
        total = 400
        workers = 8
        register_barrier = threading.Barrier(workers)
        pop_barrier = threading.Barrier(workers)
        winners, popped, errors = [], [], []
        lock = threading.Lock()

        def worker(worker_id):
            try:
                register_barrier.wait()
                for mid in range(total):
                    if registry.register(mid, f"D{mid}", mid):
                        with lock:
                            winners.append(mid)
                pop_barrier.wait()
                for mid in range(worker_id, total, workers):
                    entry = registry.pop(mid)
                    if entry is not None:
                        with lock:
                            popped.append(entry)
            except Exception as exc:  # pragma: no cover - surfaced by assertions
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(sorted(winners), list(range(total)))
        self.assertEqual(sorted(seq for _device, seq in popped), list(range(total)))
        self.assertEqual(len(registry), 0)


if __name__ == "__main__":
    unittest.main()
