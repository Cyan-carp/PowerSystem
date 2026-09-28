import unittest

from prediction.features import extract


class FeatureTests(unittest.TestCase):
    def make_samples(self):
        return [
            {"ts_ms": minute * 60_000, "temperature": 30 + minute * 0.1,
             "power": 20.0, "voltage": 400.0, "current": 29.2}
            for minute in range(30)
        ]

    def test_future_values_cannot_change_features(self):
        samples = self.make_samples()
        end = samples[-1]["ts_ms"]
        baseline = extract(samples, end)
        future = dict(samples[-1], ts_ms=end + 60_000, temperature=1000)
        self.assertEqual(baseline, extract(samples + [future], end))

    def test_incomplete_window_is_rejected(self):
        samples = self.make_samples()
        with self.assertRaises(ValueError):
            extract(samples[:20], samples[-1]["ts_ms"])


if __name__ == "__main__":
    unittest.main()
