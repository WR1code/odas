import math

import numpy as np

from avtwin_linux.shared_origin import configure_shared_origin


def _calibration() -> dict:
    angle = math.radians(30)
    cosine, sine = math.cos(angle), math.sin(angle)
    transform = np.eye(4)
    transform[:3, :3] = [[cosine, -sine, 0], [sine, cosine, 0], [0, 0, 1]]
    transform[:3, 3] = [2.0, -1.0, 0.2]
    return {
        "protocol": "AVTWIN_SPATIAL_CALIBRATION_V1",
        "source_frame_id": "phone",
        "target_frame_id": "camera_init",
        "target_from_source": transform.tolist(),
        "quality": {"accepted": True, "rmse_m": 0.05},
    }


def _pose(position, yaw_degrees=0, frame="phone") -> dict:
    half = math.radians(yaw_degrees) / 2
    return {
        "position_m": list(position),
        "orientation_xyzw": [0, 0, math.sin(half), math.cos(half)],
        "frame_id": frame,
    }


def test_linux_microphone_mode_places_microphone_at_zero() -> None:
    result = configure_shared_origin(
        _calibration(), mode="linux_microphone",
        phone_origin_pose=_pose((1, 0, 0)),
        linux_microphone_pose=_pose((4, 2, 0.2), frame="camera_init"),
    )
    assert result["phone_reset_required"] is False
    assert np.allclose(result["linux_microphone_position_m"], [0, 0, 0], atol=1e-9)
    assert not np.allclose(result["phone_position_m"], [0, 0, 0])
    matrix = np.asarray(result["derived_calibration"]["target_from_source"])
    phone_position = matrix @ np.array([1, 0, 0, 1])
    assert np.allclose(phone_position[:3], result["phone_position_m"])


def test_iphone_mode_places_phone_at_zero_and_reports_linux_relative_position() -> None:
    result = configure_shared_origin(
        _calibration(), mode="iphone_current",
        phone_origin_pose=_pose((1, 0.5, 0), yaw_degrees=20),
        linux_microphone_pose=_pose((4, 2, 0.2), frame="camera_init"),
    )
    assert result["phone_reset_required"] is True
    assert np.allclose(result["phone_position_m"], [0, 0, 0], atol=1e-9)
    assert not np.allclose(result["linux_microphone_position_m"], [0, 0, 0])
    assert np.allclose(result["derived_calibration"]["target_from_source"], np.eye(4))
    assert np.allclose(
        result["next_phone_source_to_world"], result["world_from_shared_origin"]
    )


def test_second_switch_uses_the_post_reset_phone_source_transform() -> None:
    first = configure_shared_origin(
        _calibration(), mode="iphone_current",
        phone_origin_pose=_pose((1, 0.5, 0), yaw_degrees=20),
        linux_microphone_pose=_pose((4, 2, 0.2), frame="camera_init"),
    )
    second_phone_pose = _pose((0.4, -0.2, 0), yaw_degrees=10)
    second = configure_shared_origin(
        _calibration(), mode="linux_microphone",
        phone_origin_pose=second_phone_pose,
        linux_microphone_pose=_pose((4, 2, 0.2), frame="camera_init"),
        phone_source_to_world=first["next_phone_source_to_world"],
    )
    expected_world_phone = (
        np.asarray(first["next_phone_source_to_world"])
        @ np.array([0.4, -0.2, 0, 1])
    )
    expected_shared_phone = (
        np.linalg.inv(np.asarray(second["world_from_shared_origin"]))
        @ expected_world_phone
    )
    assert np.allclose(second["phone_position_m"], expected_shared_phone[:3])
    assert np.allclose(second["linux_microphone_position_m"], [0, 0, 0])
