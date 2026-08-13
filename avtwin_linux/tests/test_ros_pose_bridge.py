from types import SimpleNamespace

from avtwin_linux.ros_pose_bridge import pose_message, rebase_pose_message


def _pose():
    return SimpleNamespace(
        position=SimpleNamespace(x=1.0, y=2.0, z=3.0),
        orientation=SimpleNamespace(x=0.0, y=0.0, z=0.5, w=0.5),
    )


def test_odometry_is_translated_with_callback_monotonic_time() -> None:
    message = SimpleNamespace(
        header=SimpleNamespace(frame_id="map"), child_frame_id="body",
        pose=SimpleNamespace(pose=_pose()),
    )
    result = pose_message(
        message, "odometry", received_monotonic_ns=123456,
        source="fast_lio", fallback_child_frame_id="livox_frame",
    )
    assert result["protocol"] == "AVTWIN_POSE_V1"
    assert result["timestamp_basis"] == "monotonic_ns"
    assert result["timestamp_ns"] == 123456
    assert result["position_m"] == [1.0, 2.0, 3.0]
    assert result["orientation_xyzw"] == [0.0, 0.0, 0.5, 0.5]
    assert result["frame_id"] == "map"
    assert result["child_frame_id"] == "body"


def test_pose_stamped_uses_lidar_fallback_child_frame() -> None:
    message = SimpleNamespace(header=SimpleNamespace(frame_id="world"), pose=_pose())
    result = pose_message(
        message, "pose_stamped", received_monotonic_ns=9,
        source="slam", fallback_child_frame_id="livox_frame",
    )
    assert result["frame_id"] == "world"
    assert result["child_frame_id"] == "livox_frame"


def test_pose_is_rebased_to_first_bridge_sample() -> None:
    origin = {
        "position_m": [10.0, 20.0, 2.0],
        "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
        "frame_id": "camera_init", "timestamp_ns": 100,
    }
    first = rebase_pose_message(origin, origin)
    assert first["position_m"] == [0.0, 0.0, 0.0]
    assert first["orientation_xyzw"] == [0.0, 0.0, 0.0, 1.0]
    moved = rebase_pose_message({
        **origin, "position_m": [11.5, 18.0, 2.25], "timestamp_ns": 200,
    }, origin)
    assert moved["position_m"] == [1.5, -2.0, 0.25]
    assert moved["frame_id"] == "camera_init/relative_start"
    assert moved["coordinate_mode"] == "relative_to_bridge_start"
