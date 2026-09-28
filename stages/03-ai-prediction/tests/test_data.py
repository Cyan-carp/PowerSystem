import tempfile
import unittest
from datetime import date
from pathlib import Path

from prediction.generate import generate
from prediction.train import load_windows


class DatasetTests(unittest.TestCase):
    def test_events_produce_future_positive_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = generate(root, 10, date(2026, 1, 1), 2026)
            days, features, labels = load_windows(root)
            self.assertEqual(manifest["fault_events"], 30)
            self.assertEqual(len(set(days)), 10)
            self.assertEqual(features.shape[1], 15)
            self.assertEqual(int(labels.sum()), 360)
            self.assertGreater(len(labels), 8000)


if __name__ == "__main__":
    unittest.main()
