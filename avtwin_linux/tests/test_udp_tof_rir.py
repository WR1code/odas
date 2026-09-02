from __future__ import annotations

import json
import socket
import threading
import time

import numpy as np

from avtwin_linux.rir import estimate_rirs
from avtwin_linux.diagnostics import test_udp_roundtrip as run_udp_roundtrip_test
from avtwin_linux.tof import calculate_tof
from avtwin_linux.udp_listener import UdpListener


def test_udp_preserves_unknown_fields_and_raw_json() -> None:
    listener = UdpListener("127.0.0.1", 0)
    # Bind an explicitly free port because the public listener records its configured port.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    listener.port = port
    listener.start()
    time.sleep(0.05)
    payload = {"status": "c2_play_issued", "t3_precise": False, "future_field": [1, 2, 3]}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(json.dumps(payload).encode(), ("127.0.0.1", port))
    deadline = time.monotonic() + 1.0
    while not listener.messages and time.monotonic() < deadline:
        time.sleep(0.01)
    listener.stop()
    assert listener.messages[0]["future_field"] == [1, 2, 3]
    assert json.loads(listener.raw_lines[0])["raw"] == json.dumps(payload)


def test_linux_udp_roundtrip_requires_matching_android_reply() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as remote:
        remote.bind(("127.0.0.1", 0))
        remote_port = remote.getsockname()[1]
        remote.settimeout(1.0)

        def android_echo() -> None:
            payload, source = remote.recvfrom(8192)
            ping = json.loads(payload.decode())
            remote.sendto(json.dumps({
                "protocol": "AVTWIN_UDP_TEST_V1",
                "type": "udp_test_reply",
                "nonce": "wrong-nonce",
                "receiver": "android-test",
            }).encode(), source)
            reply = {
                "protocol": "AVTWIN_UDP_TEST_V1",
                "type": "udp_test_reply",
                "nonce": ping["nonce"],
                "receiver": "android-test",
            }
            remote.sendto(json.dumps(reply).encode(), source)

        worker = threading.Thread(target=android_echo, daemon=True)
        worker.start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as local_probe:
            local_probe.bind(("127.0.0.1", 0))
            local_port = local_probe.getsockname()[1]
        result = run_udp_roundtrip_test(
            "127.0.0.1", local_port, "127.0.0.1", remote_port, timeout=1.0,
        )
        worker.join(timeout=1.0)

    assert result["success"] is True
    assert result["received"]["receiver"] == "android-test"
    assert result["roundtrip_ms"] >= 0.0
    assert result["observed_packets"][0]["matched"] is False
    assert result["observed_packets"][1]["matched"] is True


def test_normal_linux_listener_replies_to_android_udp_test_ping() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    listener = UdpListener("127.0.0.1", port)
    listener.start()
    try:
        ping = {
            "protocol": "AVTWIN_UDP_TEST_V1",
            "type": "udp_test_ping",
            "nonce": "android-nonce-1",
        }
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.settimeout(1.0)
            sender.sendto(json.dumps(ping).encode(), ("127.0.0.1", port))
            payload, _source = sender.recvfrom(8192)
        reply = json.loads(payload.decode())
    finally:
        listener.stop()

    assert reply == {
        "protocol": "AVTWIN_UDP_TEST_V1",
        "type": "udp_test_reply",
        "nonce": "android-nonce-1",
        "receiver": "linux",
    }
    assert listener.messages[0]["automatic_test_reply_sent"] is True


def test_listener_sends_arm_from_its_bound_result_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener_probe:
        listener_probe.bind(("127.0.0.1", 0))
        linux_port = listener_probe.getsockname()[1]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as android:
        android.bind(("127.0.0.1", 0))
        android.settimeout(1.0)
        android_port = android.getsockname()[1]
        listener = UdpListener("127.0.0.1", linux_port)
        listener.start()
        try:
            listener.send_json("127.0.0.1", android_port, {"type": "arm"})
            _payload, source = android.recvfrom(8192)
        finally:
            listener.stop()
    assert source[1] == linux_port


def test_udp_missing_still_reports_tof_unavailable() -> None:
    result = calculate_tof(4_800, 48_000, [], 343.0, None)
    assert not result["available"]
    assert "Android precise reply delay" in result["reason"]


def test_precise_reply_delay_interface_and_uncalibrated_label() -> None:
    result = calculate_tof(
        4_800, 48_000,
        [{"t3_precise": True, "reply_delay_samples": 2_400, "sample_rate": 48_000}],
        343.0, None,
    )
    assert result["available"]
    assert abs(result["tof_seconds"] - 0.025) < 1e-12
    assert result["calibration"] == "preliminary / uncalibrated"


def test_non_precise_android_delay_never_creates_exact_tof() -> None:
    result = calculate_tof(
        4_800, 48_000,
        [{"t3_precise": False, "reply_delay_samples": 2_400, "sample_rate": 48_000}],
        343.0, None,
    )
    assert not result["available"]
    assert result["exact_tof"] == "NOT AVAILABLE"


def test_rir_zero_channel_and_both_methods() -> None:
    rng = np.random.default_rng(4)
    probe = rng.normal(size=512).astype(np.float32)
    recording = np.zeros((4_000, 8), dtype=np.float32)
    arrival = 400
    recording[arrival : arrival + probe.size, :7] = probe[:, None]
    statuses = {str(ch): ("inactive_zero" if ch == 7 else "active") for ch in range(8)}
    for method in ("correlation", "correlation_paper", "deconv"):
        rirs, info = estimate_rirs(recording, probe, arrival, statuses, method=method, duration=0.02)
        assert rirs.shape == (960, 8)
        assert info["channels"]["7"]["status"] == "inactive_zero"
        assert np.max(np.abs(rirs[:, 0])) > 0
