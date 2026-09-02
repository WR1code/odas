import os
from pathlib import Path
import tempfile
import unittest

import numpy as np

from simulation.acoustix_service.client import compute_rirs
from simulation.acoustix_service.scene_converter import convert_scene
from simulation.common.geometry import Pose, UMA8_ACTIVE_MICS_M, transform_points
from simulation.common.validation import validate_direct_delays


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("RUN_ACOUSTIX_TESTS") == "1", "set RUN_ACOUSTIX_TESTS=1 for the GPU integration test")
class OfficialAcoustixTests(unittest.TestCase):
    def test_direct_arrival_and_tdoa_within_one_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            scene_json = ROOT / "simulation/scenes/test_room.json"
            xml = convert_scene(scene_json, ROOT / "simulation/configs/materials.json", output / "scene")
            source = np.asarray([1.8, 0.7, 1.2])
            pose = Pose.from_values([0, 0, 1.2], [1, 0, 0, 0])
            microphones = transform_points(pose, UMA8_ACTIVE_MICS_M)
            rirs, status = compute_rirs(scene_xml=xml, config=ROOT / "simulation/configs/acoustix_48k.yml",
                source_position_m=source, receiver_positions_m=microphones, output_npz=output / "rirs.npz",
                seed=20260809, project_root=ROOT, materials_path=ROOT / "simulation/configs/materials.json")
            self.assertEqual(status["backend"], "official_acoustix")
            metrics = validate_direct_delays(rirs, source, microphones)
            self.assertLessEqual(metrics["max_delay_error_samples"], 1.0)
            self.assertLessEqual(metrics["max_tdoa_error_samples"], 1.0)


if __name__ == "__main__":
    unittest.main()
