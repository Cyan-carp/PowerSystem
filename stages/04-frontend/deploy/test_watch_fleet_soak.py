"""Offline checks only: no credentials, Docker or network."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('soak', Path(__file__).with_name('watch-fleet-soak.py'))
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)


class CoverageTests(unittest.TestCase):
    def test_expected_scopes(self):
        for scope, count in soak.EXPECTED_DEVICES.items():
            with self.subTest(scope=scope):
                self.assertFalse(soak.coverage_incomplete(dict(scope=scope, expected=count,
                    registered=count, observed=count, partial=False)))

    def test_shrunk_manifest_is_not_success(self):
        self.assertTrue(soak.coverage_incomplete(dict(scope='business', expected=0,
            registered=0, observed=0, partial=False)))

    def test_partial_or_missing_observations(self):
        for observed, partial in [(139, True), (140, True), (141, False)]:
            self.assertTrue(soak.coverage_incomplete(dict(scope='business', expected=140,
                registered=140, observed=observed, partial=partial)))

    def test_percentiles(self):
        self.assertEqual(soak.percentile(list(range(1, 101)), .95), 95)
        self.assertIsNone(soak.percentile([], .95))


if __name__ == '__main__':
    unittest.main()
