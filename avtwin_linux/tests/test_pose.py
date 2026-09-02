from __future__ import annotations

import math
import json
import socket
import time

import pytest

from avtwin_linux.pose import (
    ManualPoseProvider, PoseSample, PoseTimeline, parse_vector3, rotate_vector, transform_offset,
    UdpPoseProvider,
)


def test_offset_rotates_with_lidar_orientation() -> None:
    half = math.sqrt(0.5)
    pose = PoseSample(
        timestamp_ns=1_000_000_000,
        position_m=(10.0, 20.0, 1.0),
        orientation_xyzw=(0.0, 0.0, half, half),
    )
    assert rotate_vector(pose.orientation_xyzw, (1.0, 0.0, 0.0)) == pytest.approx((0.0, 1.0, 0.0))
    speaker = transform_offset(pose, (1.0, 0.0, 0.0), child_frame_id="speaker")
    microphone = transform_offset(pose, (0.0, 1.0, 0.0), child_frame_id="microphone")
    assert speaker["position_m"] == pytest.approx((10.0, 21.0, 1.0))
    assert microphone["position_m"] == pytest.approx((9.0, 20.0, 1.0))


def test_pose_timeline_interpolates_position_and_orientation() -> None:
    timeline = PoseTimeline()
    timeline.add(PoseSample(1_000_000_000, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)))
    timeline.add(PoseSample(1_200_000_000, (2.0, 4.0, 0.0), (0.0, 0.0, 0.0, 1.0)))
    pose, info = timeline.pose_at(1_100_000_000, max_age_ns=150_000_000)
    assert pose is not None
    assert pose.position_m == pytest.approx((1.0, 2.0, 0.0))
    assert info["method"] == "interpolated"
    assert info["interpolation_gap_ms"] == 200.0


def test_pose_message_uses_only_local_monotonic_timestamp() -> None:
    now = time.monotonic_ns()
    sample = PoseSample.from_message({
        "protocol": "AVTWIN_POSE_V1", "type": "lidar_pose",
        "timestamp_basis": "monotonic_ns", "timestamp_ns": now,
        "position_m": [1, 2, 3], "orientation_xyzw": [0, 0, 0, 1],
    }, now + 100)
    assert sample.timestamp_ns == now
    with pytest.raises(ValueError, match="monotonic_ns"):
        PoseSample.from_message({
            "type": "lidar_pose", "timestamp_basis": "unix_ns", "timestamp_ns": now,
            "position_m": [1, 2, 3], "orientation_xyzw": [0, 0, 0, 1],
        }, now + 100)


def test_vector_parser_rejects_invalid_extrinsics() -> None:
    assert parse_vector3("0.1,-0.2,0.3") == (0.1, -0.2, 0.3)
    with pytest.raises(ValueError):
        parse_vector3("0.1,0.2")


def test_manual_pose_holds_each_coordinate_from_its_application_time() -> None:
    provider = ManualPoseProvider((1.0, 2.0, 3.0), initial_timestamp_ns=100)
    provider.update((4.0, 5.0, 6.0), timestamp_ns=200)

    before, before_info = provider.pose_at(150)
    after, after_info = provider.pose_at(250)

    assert before.position_m == pytest.approx((1.0, 2.0, 3.0))
    assert after.position_m == pytest.approx((4.0, 5.0, 6.0))
    assert before.tracking_status == "MANUAL"
    assert before_info["method"] == after_info["method"] == "manual_hold"
    assert provider.metadata()["source"] == "manual"


def test_manual_pose_reset_sets_current_coordinate_to_zero() -> None:
    provider = ManualPoseProvider((8.0, -2.0, 0.5))
    provider.reset_origin()
    assert provider.latest().position_m == pytest.approx((0.0, 0.0, 0.0))


def test_udp_pose_provider_receives_protocol_message() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    provider = UdpPoseProvider("127.0.0.1", port, max_age_s=1.0)
    provider.start()
    try:
        message = {
            "protocol": "AVTWIN_POSE_V1", "type": "lidar_pose",
            "position_m": [1.0, 2.0, 3.0],
            "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "frame_id": "map", "tracking_status": "TRACKING",
        }
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(json.dumps(message).encode(), ("127.0.0.1", port))
        deadline = time.monotonic() + 1.0
        while provider.latest() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        latest = provider.latest()
        assert latest is not None
        assert latest.position_m == pytest.approx((0.0, 0.0, 0.0))
        assert latest.timestamp_basis == "udp_receive_monotonic_ns"
        assert provider.metadata()["received"] == 1

        message["position_m"] = [1.2, 1.9, 3.05]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(json.dumps(message).encode(), ("127.0.0.1", port))
        deadline = time.monotonic() + 1.0
        while provider.metadata()["received"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert provider.latest() is not None
        assert provider.latest().position_m == pytest.approx((0.2, -0.1, 0.05))

        # A failed SLAM update must not replace the last plausible location with
        # the hundreds-of-metres jump that the GUI used to display.
        message["position_m"] = [501.2, -999.1, 303.05]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(json.dumps(message).encode(), ("127.0.0.1", port))
        deadline = time.monotonic() + 1.0
        while provider.metadata()["rejected"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert provider.metadata()["rejected"] == 1
        assert "SLAM 位姿异常跳变" in (provider.metadata()["last_rejection"] or "")
        assert provider.latest() is not None
        assert provider.latest().position_m == pytest.approx((0.2, -0.1, 0.05))

        provider.reset_origin()
        message["position_m"] = [50.0, -70.0, 120.0]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(json.dumps(message).encode(), ("127.0.0.1", port))
        deadline = time.monotonic() + 1.0
        while provider.metadata()["received"] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert provider.latest() is not None
        assert provider.latest().position_m == pytest.approx((0.0, 0.0, 0.0))
    finally:
        provider.stop()
