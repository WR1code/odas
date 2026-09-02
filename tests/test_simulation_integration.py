import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from scipy.signal import fftconvolve

from simulation.acoustix_service.scene_converter import convert_scene
from simulation.common.audio import float_to_s32le, with_zero_hardware_channel
from simulation.common.geometry import Pose, UMA8_ACTIVE_MICS_M, spherical_from_vector, transform_points
from simulation.common.odas import iter_json_objects
from simulation.common.validation import validate_direct_delays
from simulation.odas_streamer.dynamic import CrossfadingConvolver, FIRBlock, RIRCache, RIRUpdatePolicy


ROOT = Path(__file__).resolve().parents[1]


class GeometryTests(unittest.TestCase):
    def test_uma_transform_and_azimuth_convention(self):
        pose = Pose.from_values([1, 2, 3], [1, 0, 0, 0])
        world = transform_points(pose, UMA8_ACTIVE_MICS_M)
        np.testing.assert_allclose(world[0], [1, 2, 3])
        self.assertAlmostEqual(spherical_from_vector([1, 0, 0])["azimuth_deg"], 0.0)
        self.assertAlmostEqual(spherical_from_vector([0, 1, 0])["azimuth_deg"], 90.0)
        self.assertAlmostEqual(spherical_from_vector([0, -1, 0])["azimuth_deg"], -90.0)

    def test_direct_and_tdoa_one_sample_contract(self):
        source = np.asarray([1.0, 0.2, 0.0])
        mics = UMA8_ACTIVE_MICS_M[:3]
        expected = np.linalg.norm(mics - source, axis=1) / 343.8 * 48000
        rirs = np.zeros((3, 512))
        for channel, sample in enumerate(expected):
            rirs[channel, int(round(sample))] = 1.0
        result = validate_direct_delays(rirs, source, mics)
        self.assertLessEqual(result["max_delay_error_samples"], 1.0)
        self.assertLessEqual(result["max_tdoa_error_samples"], 1.0)


class PCMTests(unittest.TestCase):
    def test_channel_order_width_endianness_and_zero_eighth(self):
        active = np.zeros((8, 7), dtype=float)
        for channel in range(7):
            active[channel, channel] = (channel + 1) / 10.0
        audio = with_zero_hardware_channel(active)
        raw = float_to_s32le(audio).tobytes()
        decoded = np.frombuffer(raw, dtype="<i4").reshape(8, 8)
        self.assertEqual(len(raw), 8 * 8 * 4)
        self.assertTrue(np.all(decoded[:, 7] == 0))
        for channel in range(7):
            self.assertGreater(decoded[channel, channel], 0)
            self.assertEqual(np.count_nonzero(decoded[channel]), 1)


class StreamingTests(unittest.TestCase):
    def test_block_fir_matches_full_convolution(self):
        rng = np.random.default_rng(7)
        signal = rng.normal(size=96)
        rirs = rng.normal(size=(2, 13))
        block = FIRBlock(rirs)
        output = np.concatenate([block.process(signal[i:i+16]) for i in range(0, len(signal), 16)])
        expected = np.stack([fftconvolve(signal, rir)[:len(signal)] for rir in rirs], axis=1)
        np.testing.assert_allclose(output, expected, atol=1e-10)

    def test_crossfade_has_no_step_for_identical_rir(self):
        rir = np.asarray([[1.0, 0.25]])
        convolver = CrossfadingConvolver(rir, fade_samples=16)
        first = convolver.process(np.ones(16))
        convolver.update(rir.copy())
        second = convolver.process(np.ones(16))
        self.assertLess(abs(second[0, 0] - first[-1, 0]), 1e-12)

    def test_rir_policy_detects_rotation_without_translation(self):
        cache = RIRCache(RIRUpdatePolicy(position_threshold_m=0.05, angle_threshold_deg=3.0))
        mics = UMA8_ACTIVE_MICS_M.copy()
        cache.put(np.asarray([1.0, 0.0, 0.0]), mics, np.ones((7, 4)))
        angle = np.radians(5.0)
        rotation = np.asarray([[np.cos(angle), -np.sin(angle), 0.0],
                               [np.sin(angle), np.cos(angle), 0.0], [0.0, 0.0, 1.0]])
        self.assertTrue(cache.pose_changed(np.asarray([1.0, 0.0, 0.0]), mics @ rotation.T))


class ConversionTests(unittest.TestCase):
    def test_scene_conversion_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            xml = convert_scene(ROOT / "simulation/scenes/test_room.json",
                                ROOT / "simulation/configs/materials.json", output)
            first = xml.read_bytes()
            xml = convert_scene(ROOT / "simulation/scenes/test_room.json",
                                ROOT / "simulation/configs/materials.json", output)
            self.assertEqual(first, xml.read_bytes())
            self.assertEqual(len(list(output.glob("*.ply"))), 8)

    def test_concatenated_odas_json(self):
        values = list(iter_json_objects('{"timeStamp":1,"src":[]}\n{"timeStamp":2,"src":[]}'))
        self.assertEqual([value["timeStamp"] for value in values], [1, 2])


if __name__ == "__main__":
    unittest.main()
