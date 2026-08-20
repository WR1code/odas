import math
from pathlib import Path

import numpy as np

from avtwin_linux.spatial_calibration import (
    apply_transform, calibrate_point_clouds, read_avpc, write_avpc,
)
from avtwin_linux.calibration_server import CalibrationService


def _room(seed: int = 4) -> np.ndarray:
    rng = np.random.default_rng(seed)
    count = 900
    floor = np.column_stack((rng.uniform(-3, 4, count), rng.uniform(-2, 3, count), np.zeros(count)))
    wall_x = np.column_stack((np.full(count, -3.0), rng.uniform(-2, 3, count), rng.uniform(0, 2.7, count)))
    wall_y = np.column_stack((rng.uniform(-3, 4, count), np.full(count, 3.0), rng.uniform(0, 2.7, count)))
    box = rng.uniform([0.3, -0.9, 0.2], [1.4, 0.2, 1.6], size=(500, 3))
    return np.vstack((floor, wall_x, wall_y, box))


def test_avpc_round_trip(tmp_path: Path) -> None:
    points = _room()[:100]
    path = tmp_path / "map.avpc"
    write_avpc(path, points, {"frame_id": "phone"})
    restored, metadata = read_avpc(path)
    assert metadata["frame_id"] == "phone"
    assert np.allclose(restored, points, atol=1e-6)


def test_gravity_aligned_registration_recovers_transform() -> None:
    target = _room()
    angle = math.radians(37.0)
    rotation = np.array([
        [math.cos(angle), -math.sin(angle), 0],
        [math.sin(angle), math.cos(angle), 0],
        [0, 0, 1],
    ])
    expected = np.eye(4)
    expected[:3, :3] = rotation
    expected[:3, 3] = [1.2, -0.65, 0.35]
    inverse = np.linalg.inv(expected)
    # A phone scan observes only a subset of the stationary LiDAR map.
    source = apply_transform(target[target[:, 0] < 2.2], inverse)
    result = calibrate_point_clouds(source, target, voxel_size_m=0.1, yaw_step_degrees=10)
    actual = np.asarray(result.target_from_source)
    assert result.quality.accepted, result.quality.reason
    assert np.linalg.norm(actual[:3, 3] - expected[:3, 3]) < 0.08
    yaw_error = math.atan2(actual[1, 0], actual[0, 0]) - angle
    assert abs(math.atan2(math.sin(yaw_error), math.cos(yaw_error))) < math.radians(2)


def test_service_only_activates_accepted_transform(tmp_path: Path) -> None:
    target = _room()
    lidar = tmp_path / "lidar.avpc"
    phone = tmp_path / "upload.avpc"
    write_avpc(lidar, target, {"frame_id": "mid360_map"})
    transform = np.eye(4)
    transform[:3, :3] = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
    transform[:3, 3] = [0.8, -0.3, 0.2]
    write_avpc(phone, apply_transform(target, np.linalg.inv(transform)), {
        "frame_id": "arkit_user_origin_x_forward_y_left_z_up",
    })
    service = CalibrationService(tmp_path / "state", lidar)
    response = service.accept_phone_map(phone.read_bytes())
    assert response["result"]["quality"]["accepted"] is True
    assert service.result.is_file()
