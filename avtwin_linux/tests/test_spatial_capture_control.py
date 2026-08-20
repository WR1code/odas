from pathlib import Path
import sys
import threading

from avtwin_linux.spatial_capture_control import SpatialCaptureController
from avtwin_linux.udp_listener import validate_mobile_session_command


def test_capture_is_idempotent_and_rejects_parallel_command(tmp_path: Path) -> None:
    output = tmp_path / "lidar.avpc"
    release = tmp_path / "release"
    script = (
        "import pathlib,sys,time\n"
        "from avtwin_linux.spatial_calibration import write_avpc\n"
        "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
        "write_avpc(pathlib.Path(sys.argv[1]), [[i/10, (i%9)/10, (i%7)/10] for i in range(100)])\n"
    )
    controller = SpatialCaptureController(
        Path.cwd(), output_path=output, calibration_port=0,
        capture_command=lambda _command, _duration: [
            sys.executable, "-c", script, str(output), str(release),
        ],
    )
    first = controller.request_start("one")
    duplicate = controller.request_start("one")
    busy = controller.request_start("two")
    assert first.accepted and first.state == "capturing"
    assert duplicate.accepted and duplicate.reason == "duplicate_request_reack"
    assert not busy.accepted and busy.state == "busy"
    release.touch()
    for _ in range(200):
        cached = controller.request_start("one")
        if cached.state == "completed":
            break
        threading.Event().wait(0.01)
    assert cached.state == "completed"
    assert cached.point_count == 100
    assert controller.request_start("one") == cached
    controller.close()


def test_invalid_duration_does_not_start(tmp_path: Path) -> None:
    controller = SpatialCaptureController(
        Path.cwd(), output_path=tmp_path / "map.avpc",
        capture_command=lambda _command, _duration: ["must-not-run"],
    )
    result = controller.request_start("bad", duration_seconds=1)
    assert not result.accepted
    assert result.reason == "duration_out_of_range_3_to_60_seconds"


def test_ios_map_command_reuses_trusted_mobile_control_envelope() -> None:
    message = {
        "type": "lidar_map_capture_start_request",
        "protocol_version": 1,
        "command_id": "map-1",
        "linux_result_port": 5005,
        "mobile_control_port": 5006,
        "source": "192.168.1.20:43123",
    }
    accepted, reason, host, reply_port = validate_mobile_session_command(
        message,
        expected_type="lidar_map_capture_start_request",
        expected_host="192.168.1.20",
        linux_result_port=5005,
        mobile_control_port=5006,
    )
    assert accepted and reason == "accepted"
    assert host == "192.168.1.20" and reply_port == 5006
    message["source"] = "192.168.1.99:43123"
    accepted, reason, _host, _port = validate_mobile_session_command(
        message,
        expected_type="lidar_map_capture_start_request",
        expected_host="192.168.1.20",
        linux_result_port=5005,
        mobile_control_port=5006,
    )
    assert not accepted and reason == "source_host_mismatch"
