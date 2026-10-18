import json
import math
import unittest
import tempfile
from datetime import datetime
from pathlib import Path

from simulator import CHINA_TZ, DEVICES, Ledger, daylight_profile_ms, fault_ramp, make_sample


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

    def fault_timestamp(self, device):
        for minute in range(8 * 60, 16 * 60):
            stamp = datetime(2026, 10, 15, minute // 60, minute % 60, tzinfo=CHINA_TZ)
            ramp, active = fault_ramp(device, stamp, 2026)
            if active and ramp > 0:
                return int(stamp.timestamp() * 1000), ramp
        self.fail('fixture must contain a scheduled fault')

    def test_profile_fault_window_does_not_replay_fault_at_real_night(self):
        for device in DEVICES:
            profile, _ = self.fault_timestamp(device)
            raw, active = make_sample(device, 10, self.night_ms, 2026, 'clock-test', profile_ts_ms=profile)
            self.assertFalse(active)
            self.assertGreater(json.loads(raw)['power'], 20)

    def test_scheduled_fault_uses_sample_clock_not_daylight_clock(self):
        profile = daylight_profile_ms(self.night_ms, 0, 60)
        for device in DEVICES:
            timestamp, ramp = self.fault_timestamp(device)
            scheduled, active = make_sample(device, 10, timestamp, 2026, 'clock-test', profile_ts_ms=profile)
            normal, off = make_sample(device, 10, timestamp, 2026, 'clock-test', profile_ts_ms=profile, fault_mode='off')
            self.assertTrue(active)
            self.assertFalse(off)
            self.assertAlmostEqual(json.loads(scheduled)['temperature'] - json.loads(normal)['temperature'], ramp, delta=.0011)

    def test_off_keeps_power_and_identity_unchanged(self):
        for device in DEVICES:
            timestamp, _ = self.fault_timestamp(device)
            left, _ = make_sample(device, 123, timestamp, 2026, 'same-run', profile_ts_ms=self.night_ms)
            right, active = make_sample(device, 123, timestamp, 2026, 'same-run', profile_ts_ms=self.night_ms, fault_mode='off')
            a, b = json.loads(left), json.loads(right)
            a.pop('temperature'); b.pop('temperature')
            self.assertEqual(a, b)
            self.assertFalse(active)

    def test_default_scheduled_preserves_natural_sample_behavior(self):
        for device in DEVICES:
            timestamp, _ = self.fault_timestamp(device)
            self.assertEqual(make_sample(device, 10, timestamp, 2026, 'natural'),
                             make_sample(device, 10, timestamp, 2026, 'natural', fault_mode='scheduled'))

    def test_off_stays_off_across_multiple_daylight_loops(self):
        for device in DEVICES:
            for minute in range(0, 3 * 60, 5):
                stamp = self.night_ms + minute * 60_000
                profile = daylight_profile_ms(stamp, minute * 60, 60)
                raw, active = make_sample(device, minute, stamp, 2026, 'loop-test', profile_ts_ms=profile, fault_mode='off')
                self.assertFalse(active)
                self.assertGreater(json.loads(raw)['power'], 20)

    def test_invalid_fault_mode_rejected(self):
        for mode in ('profile', '', None, 'OFF'):
            with self.assertRaises(ValueError):
                make_sample(DEVICES[0], 1, self.night_ms, 2026, 'invalid', fault_mode=mode)

    def test_mode_change_retains_ledger_queue_and_fault_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'simulator.sqlite'
            device = DEVICES[0]
            stamp, _ = self.fault_timestamp(device)
            ledger = Ledger(path)
            raw, active = make_sample(device, 0, stamp, 2026, 'same-run')
            ledger.add(device, 0, stamp, raw, active)
            ledger.db.close()
            resumed = Ledger(path)
            self.assertEqual(resumed.next_seq(device), 1)
            self.assertEqual(resumed.counts(), (1, 1))
            new_raw, new_active = make_sample(device, 1, stamp + 5000, 2026, 'same-run', fault_mode='off')
            resumed.add(device, 1, stamp + 5000, new_raw, new_active)
            self.assertEqual(resumed.db.execute('SELECT fault_active FROM generated ORDER BY seq').fetchall(), [(1,), (0,)])
            resumed.acknowledge(device, 0)
            self.assertEqual(resumed.counts(), (2, 1))
            self.assertEqual(resumed.pending(device)[0][0], 1)
            resumed.db.close()


if __name__ == "__main__":
    unittest.main()
