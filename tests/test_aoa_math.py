import math
import unittest

from uma8_visualizer.aoa_math import DirectionSmoother, angle_degrees, calibrated_angle


class AngleMathTests(unittest.TestCase):
    def test_cardinal_directions(self) -> None:
        self.assertAlmostEqual(angle_degrees(1, 0), 0.0)
        self.assertAlmostEqual(angle_degrees(0, 1), 90.0)
        self.assertAlmostEqual(angle_degrees(-1, 0), 180.0)
        self.assertAlmostEqual(angle_degrees(0, -1), 270.0)

    def test_measured_direction(self) -> None:
        self.assertAlmostEqual(angle_degrees(0.988, -0.157), 351.0, delta=0.2)

    def test_angle_offset_wraps(self) -> None:
        angle = calibrated_angle(math.cos(math.radians(351)), math.sin(math.radians(351)), 9)
        self.assertAlmostEqual(angle, 0.0, places=7)

    def test_smoothing_across_north(self) -> None:
        smoother = DirectionSmoother(alpha=0.5)
        for degrees in (359.0, 1.0):
            smoother.update(math.cos(math.radians(degrees)), math.sin(math.radians(degrees)))
        angle = smoother.angle
        self.assertIsNotNone(angle)
        self.assertTrue(angle is not None and (angle < 2.0 or angle > 358.0))


if __name__ == "__main__":
    unittest.main()
