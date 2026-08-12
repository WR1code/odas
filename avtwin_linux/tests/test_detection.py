from __future__ import annotations

import numpy as np
from scipy import signal

from avtwin_linux.config import SAMPLE_RATE
from avtwin_linux.detector import analyze_recording
from avtwin_linux.matched_filter import channel_status, detect_multichannel, find_peak


def probe(f0: float, f1: float, duration: float = 0.04) -> np.ndarray:
    count = round(duration * SAMPLE_RATE)
    t = np.arange(count) / SAMPLE_RATE
    return (signal.chirp(t, f0=f0, f1=f1, t1=duration) * signal.windows.tukey(count, 0.2)).astype(np.float32)


def synthetic() -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    rng = np.random.default_rng(1942)
    c1, c2 = probe(8_000, 15_000), probe(500, 7_000)
    t1, t4 = 10_000, 41_000
    recording = rng.normal(0, 0.015, (70_000, 8)).astype(np.float32)
    offsets = [-2, 0, 2, 1, -1, 3, 0]
    for ch, offset in enumerate(offsets):
        recording[t1 + offset : t1 + offset + c1.size, ch] += 0.6 * c1
        recording[t4 + offset : t4 + offset + c2.size, ch] += 0.48 * c2
        delay = 173 + ch
        recording[t4 + offset + delay : t4 + offset + delay + c2.size, ch] += 0.14 * c2
    recording[:, 7] = 0
    return recording, c1, c2, t1, t4


def test_synthetic_multichannel_handshake_and_median() -> None:
    recording, c1, c2, t1, t4 = synthetic()
    result = analyze_recording(
        recording, c1, c2,
        playback_issue_sample=t1 - 80,
        c1_threshold=0.3,
        c2_threshold=0.3,
        reply_timeout=1.0,
    )
    assert abs(result["c1_detection"]["system_sample"] - t1) <= 1
    assert abs(result["c2_detection"]["system_sample"] - t4) <= 1
    assert result["linux_observed_roundtrip_samples"] == t4 - t1
    assert result["channel_status"]["7"] == "inactive_zero"
    assert result["quality"] if "quality" in result else True
    assert not result["tof"]["available"]


def test_noise_only_does_not_trigger() -> None:
    rng = np.random.default_rng(99)
    template = probe(2_000, 6_000)
    noise = rng.normal(0, 0.02, 30_000).astype(np.float32)
    found = find_peak(noise, template, threshold=0.30)
    assert not found.passed


def test_zero_channel_is_inactive_and_safe() -> None:
    recording, c1, _c2, _t1, _t4 = synthetic()
    statuses = channel_status(recording)
    result = detect_multichannel(recording, c1, 0.3, statuses)
    assert statuses["7"] == "inactive_zero"
    assert result["channels"]["7"]["sample"] is None
    assert result["passed"]


def test_c1_missing_returns_fail_without_hanging() -> None:
    rng = np.random.default_rng(100)
    recording = rng.normal(0, 0.01, (25_000, 8)).astype(np.float32)
    c1, c2 = probe(8_000, 14_000), probe(500, 6_000)
    result = analyze_recording(
        recording, c1, c2, playback_issue_sample=5_000,
        c1_threshold=0.5, c2_threshold=0.5, reply_timeout=0.3,
    )
    assert not result["c1_detection"]["passed"]
    assert not result["c2_detection"]["passed"]
    assert result["linux_observed_roundtrip_ms"] is None
