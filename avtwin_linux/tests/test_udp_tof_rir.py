from __future__ import annotations

import json
import socket
import time

import numpy as np

from avtwin_linux.rir import estimate_rirs
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
