from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from scipy import signal

from .config import SAMPLE_RATE


@dataclass(slots=True)
class Peak:
    sample: int | None
    score: float
    passed: bool


def template_passband(template: np.ndarray, sample_rate: int = SAMPLE_RATE) -> tuple[float, float] | None:
    """Infer the probe's occupied band from 0.1–99.9% spectral energy."""
    centered = np.asarray(template, dtype=np.float64) - float(np.mean(template))
    spectrum = np.abs(np.fft.rfft(centered)) ** 2
    total = float(np.sum(spectrum))
    if total <= np.finfo(float).eps:
        return None
    cumulative = np.cumsum(spectrum) / total
    frequencies = np.fft.rfftfreq(centered.size, 1.0 / sample_rate)
    low = float(frequencies[min(int(np.searchsorted(cumulative, 0.001)), frequencies.size - 1)])
    high = float(frequencies[min(int(np.searchsorted(cumulative, 0.999)), frequencies.size - 1)])
    span = max(high - low, sample_rate / centered.size)
    low = max(20.0, low - 0.05 * span)
    high = min(sample_rate * 0.49, high + 0.05 * span)
    if high <= low or high - low >= sample_rate * 0.47:
        return None
    return low, high


def bandlimit_for_template(
    x: np.ndarray, template: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, tuple[float, float] | None]:
    passband = template_passband(template)
    if passband is None:
        return np.asarray(x), np.asarray(template), None
    sos = signal.butter(4, passband, btype="bandpass", fs=SAMPLE_RATE, output="sos")
    # Zero-phase filtering preserves the acoustic template start sample.
    filtered_x = signal.sosfiltfilt(sos, np.asarray(x, dtype=np.float64), axis=0)
    filtered_template = signal.sosfiltfilt(sos, np.asarray(template, dtype=np.float64))
    return filtered_x, filtered_template, passband


def normalized_correlation(x: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Return the absolute zero-mean normalized correlation for every valid start."""
    x64 = np.asarray(x, dtype=np.float64)
    s64 = np.asarray(template, dtype=np.float64)
    if x64.size < s64.size:
        return np.empty(0, dtype=np.float64)
    s64 = s64 - s64.mean()
    energy_s = float(np.dot(s64, s64))
    if energy_s <= np.finfo(float).eps:
        raise ValueError("检测模板能量为零")
    n = s64.size
    sums = signal.fftconvolve(x64, np.ones(n), mode="valid")
    sums2 = signal.fftconvolve(x64 * x64, np.ones(n), mode="valid")
    window_energy = np.maximum(sums2 - sums * sums / n, 0.0)
    numerator = signal.correlate(x64, s64, mode="valid", method="fft")
    denom = np.sqrt(window_energy * energy_s)
    scores = np.zeros_like(numerator)
    valid = denom > np.finfo(float).eps
    scores[valid] = np.abs(numerator[valid]) / denom[valid]
    return np.clip(scores, 0.0, 1.0)


def find_peak(
    x: np.ndarray,
    template: np.ndarray,
    threshold: float,
    start: int = 0,
    stop: int | None = None,
) -> Peak:
    start = max(0, int(start))
    latest = x.size - template.size + 1
    stop = latest if stop is None else min(int(stop), latest)
    if stop <= start:
        return Peak(None, 0.0, False)
    scores = normalized_correlation(x[start : stop + template.size - 1], template)
    if not scores.size:
        return Peak(None, 0.0, False)
    local = int(np.argmax(scores))
    score = float(scores[local])
    return Peak(start + local, score, score >= threshold)


def channel_status(recording: np.ndarray) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for channel in range(recording.shape[1]):
        peak = float(np.max(np.abs(recording[:, channel]))) if recording.size else 0.0
        statuses[str(channel)] = "inactive_zero" if peak < 1e-8 else "active"
    return statuses


def detect_multichannel(
    recording: np.ndarray,
    template: np.ndarray,
    threshold: float,
    statuses: dict[str, str],
    *,
    start: int = 0,
    stop: int | None = None,
    max_spread_ms: float = 2.0,
) -> dict[str, Any]:
    filtered_recording, filtered_template, passband = bandlimit_for_template(
        recording, template
    )
    channels: dict[str, Any] = {}
    accepted: list[int] = []
    for channel in range(recording.shape[1]):
        if statuses[str(channel)] != "active":
            peak = Peak(None, 0.0, False)
        else:
            peak = find_peak(
                filtered_recording[:, channel], filtered_template,
                threshold, start, stop,
            )
        channels[str(channel)] = asdict(peak)
        if peak.passed and peak.sample is not None:
            accepted.append(peak.sample)
    system_sample = int(np.median(accepted)) if accepted else None
    spread = max(accepted) - min(accepted) if accepted else None
    max_spread = round(max_spread_ms * SAMPLE_RATE / 1000.0)
    return {
        "system_sample": system_sample,
        "system_time_ms": None if system_sample is None else system_sample * 1000.0 / SAMPLE_RATE,
        "system_score": float(np.median([
            channels[str(c)]["score"] for c in range(recording.shape[1])
            if channels[str(c)]["passed"]
        ])) if accepted else 0.0,
        "channels": channels,
        "channels_passed": len(accepted),
        "arrival_spread_samples": spread,
        "arrival_spread_ms": None if spread is None else spread * 1000.0 / SAMPLE_RATE,
        "warning": None if spread is None or spread <= max_spread else "多通道到达时间差异常大",
        "passed": bool(accepted),
        "template_passband_hz": None if passband is None else [passband[0], passband[1]],
    }
