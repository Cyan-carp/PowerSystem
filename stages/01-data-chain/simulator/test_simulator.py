import json
import math
import unittest
from datetime import datetime

from simulator import CHINA_TZ, DEVICES, daylight_profile_ms, make_sample


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


if __name__ == "__main__":
    unittest.main()
