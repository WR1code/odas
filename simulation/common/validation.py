"""Acoustic timing validation against analytic metric geometry."""
from __future__ import annotations

import numpy as np


def direct_peak_index(rir: np.ndarray, expected_sample: float, search_radius: int = 16) -> int:
    center = int(round(expected_sample))
    start = max(0, center - search_radius)
    stop = min(len(rir), center + search_radius + 1)
    if start == stop:
        raise ValueError("expected direct path lies outside the RIR")
    return start + int(np.argmax(np.abs(rir[start:stop])))


def validate_direct_delays(rirs: np.ndarray, source_m: np.ndarray, microphones_m: np.ndarray,
                           sample_rate: int = 48000, speed_m_s: float = 343.8,
                           tolerance_samples: float = 1.0) -> dict:
    distances = np.linalg.norm(np.asarray(microphones_m) - np.asarray(source_m), axis=1)
    expected = distances / speed_m_s * sample_rate
    observed = np.asarray([direct_peak_index(rir, value) for rir, value in zip(rirs, expected)])
    errors = np.abs(observed - expected)
    if np.max(errors) > tolerance_samples:
        raise AssertionError(f"direct-path delay error {np.max(errors):.3f} samples exceeds {tolerance_samples}")
    expected_tdoa = expected[:, None] - expected[None, :]
    observed_tdoa = observed[:, None] - observed[None, :]
    tdoa_errors = np.abs(observed_tdoa - expected_tdoa)
    if np.max(tdoa_errors) > tolerance_samples:
        raise AssertionError(f"TDOA error {np.max(tdoa_errors):.3f} samples exceeds {tolerance_samples}")
    return {"expected_samples": expected.tolist(), "observed_samples": observed.tolist(),
            "max_delay_error_samples": float(np.max(errors)),
            "max_tdoa_error_samples": float(np.max(tdoa_errors))}
