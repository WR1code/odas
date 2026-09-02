from __future__ import annotations

from typing import Any

import numpy as np
from scipy import signal

from .config import SAMPLE_RATE


def estimate_rirs(
    recording: np.ndarray,
    probe: np.ndarray,
    arrival_sample: int | None,
    channel_status: dict[str, str],
    *,
    method: str = "deconv",
    duration: float = 0.5,
    regularization: float = 1e-4,
    pre_arrival: float = 0.0,
) -> tuple[np.ndarray, dict[str, Any]]:
    count = round(duration * SAMPLE_RATE)
    rirs = np.zeros((count, recording.shape[1]), dtype=np.float32)
    pre_frames = round(pre_arrival * SAMPLE_RATE)
    info: dict[str, Any] = {
        "method": method,
        "method_description": (
            "energy-normalized probe cross-correlation (paper-compatible)"
            if method == "correlation_paper" else method
        ),
        "duration_ms": duration * 1000.0,
        "pre_arrival_ms": pre_frames * 1000.0 / SAMPLE_RATE,
        "direct_arrival_index": pre_frames,
        "time_reference": "relative_to_c2_arrival; absolute t4_sample stored in result",
        "per_channel_normalization": False,
        "channels": {},
    }
    if arrival_sample is None:
        info["available"] = False
        info["reason"] = "C2 was not detected"
        return rirs, info
    segment_start = max(0, arrival_sample - pre_frames)
    missing_pre = max(0, pre_frames - arrival_sample)
    needed = probe.size + count - 1
    received = np.zeros((needed, recording.shape[1]), dtype=np.float64)
    available = recording[segment_start : segment_start + needed - missing_pre]
    received[missing_pre : missing_pre + available.shape[0]] = available
    probe64 = np.asarray(probe, dtype=np.float64)
    probe_energy = max(float(np.dot(probe64, probe64)), np.finfo(float).eps)

    for channel in range(recording.shape[1]):
        status = channel_status[str(channel)]
        if status == "inactive_zero":
            info["channels"][str(channel)] = {"status": status, "peak": 0.0}
            continue
        y = received[:, channel]
        if method in {"correlation", "correlation_paper"}:
            corr = signal.correlate(y, probe64, mode="full", method="fft") / probe_energy
            start = probe.size - 1
            estimate = corr[start : start + count]
        elif method == "deconv":
            fft_size = int(2 ** np.ceil(np.log2(needed)))
            spectrum_y = np.fft.rfft(y, fft_size)
            spectrum_s = np.fft.rfft(probe64, fft_size)
            power = np.abs(spectrum_s) ** 2
            lam = regularization * max(float(power.max()), np.finfo(float).eps)
            estimate = np.fft.irfft(spectrum_y * np.conj(spectrum_s) / (power + lam), fft_size)[:count]
        else:
            raise ValueError(f"未知 RIR 方法：{method}")
        rirs[:, channel] = np.asarray(estimate, dtype=np.float32)
        peak = float(np.max(np.abs(estimate))) if estimate.size else 0.0
        info["channels"][str(channel)] = {
            "status": "estimated" if np.isfinite(peak) else "fail",
            "peak": peak,
        }
    info["available"] = True
    return rirs, info
