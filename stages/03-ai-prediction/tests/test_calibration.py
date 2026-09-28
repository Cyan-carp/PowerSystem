"""Guard against float32 margins hiding SciPy's finite-difference step."""

import unittest

import numpy as np

from prediction.train import calibrate, fit_calibration


class CalibrationTests(unittest.TestCase):
    def test_float32_margins_are_optimized(self) -> None:
        bins = [-2, -1, 0, 1, 2]
        positive_counts = [1, 8, 30, 75, 95]
        margins = np.repeat(np.asarray(bins, dtype=np.float32), 100)
        labels = np.concatenate(
            [np.asarray([1] * positives + [0] * (100 - positives), dtype=np.int8)
             for positives in positive_counts]
        )
        initial = calibrate(margins, {"slope": 1.0, "intercept": -2.0})
        fitted = fit_calibration(margins, labels)
        calibrated = calibrate(margins, fitted)
        self.assertLess(float(np.mean((calibrated - labels) ** 2)), float(np.mean((initial - labels) ** 2)))
        self.assertNotAlmostEqual(fitted["intercept"], -2.0, places=2)


if __name__ == "__main__":
    unittest.main()
