from __future__ import annotations

from typing import Any

import numpy as np
from scipy import stats

from .config import SAMPLE_RATE


def _decay_metrics(rir: np.ndarray, direct: int) -> dict[str, float | None]:
    values = np.asarray(rir, dtype=np.float64)
    energy = values * values
    if not energy.size or float(np.sum(energy)) <= np.finfo(float).eps:
        return {"c50_db": None, "edt_s": None, "t60_s": None, "effective_decay_length_s": 0.0}
    direct = min(max(0, direct), values.size - 1)
    split = min(values.size, direct + round(0.050 * SAMPLE_RATE))
    early = float(np.sum(energy[direct:split]))
    late = float(np.sum(energy[split:]))
    c50 = None if early <= 0 else 10.0 * np.log10(early / max(late, np.finfo(float).eps))
    schroeder = np.cumsum(energy[::-1])[::-1]
    schroeder /= max(float(schroeder[direct]), np.finfo(float).eps)
    db = 10.0 * np.log10(np.maximum(schroeder, np.finfo(float).tiny))

    def extrapolate(low: float, high: float, multiplier: float) -> float | None:
        indices = np.flatnonzero((db <= low) & (db >= high) & (np.arange(db.size) >= direct))
        if indices.size < 8:
            return None
        slope, intercept, _r, _p, _stderr = stats.linregress(indices / SAMPLE_RATE, db[indices])
        if not np.isfinite(slope) or not np.isfinite(intercept) or slope >= 0:
            return None
        return float((-60.0 - intercept) / slope * multiplier)

    edt = extrapolate(0.0, -10.0, 1.0)
    t60 = extrapolate(-5.0, -35.0, 1.0)
    below = np.flatnonzero((db <= -30.0) & (np.arange(db.size) >= direct))
    decay = (int(below[0]) - direct) / SAMPLE_RATE if below.size else (values.size - direct) / SAMPLE_RATE
    return {
        "c50_db": None if c50 is None else float(c50),
        "edt_s": edt,
        "t60_s": t60,
        "effective_decay_length_s": float(max(0.0, decay)),
    }


def assess_quality(
    recording: np.ndarray,
    rirs: np.ndarray,
    c1_detection: dict[str, Any],
    c2_detection: dict[str, Any],
    *,
    direct_index: int,
    min_channels: int = 2,
    tof_available: bool = False,
    overall_policy: str = "strict",
) -> dict[str, Any]:
    reasons: list[str] = []
    c1_channels = int(c1_detection.get("channels_passed", 0))
    c2_channels = int(c2_detection.get("channels_passed", 0))
    protocol_pass = c1_channels >= min_channels and c2_channels >= min_channels
    if c1_channels < min_channels:
        reasons.append(f"C1 有效通道不足：{c1_channels} < {min_channels}")
    if c2_channels < min_channels:
        reasons.append(f"C2 有效通道不足：{c2_channels} < {min_channels}")
    spread_ms = c2_detection.get("arrival_spread_ms")
    consistent = spread_ms is not None and float(spread_ms) <= 2.0
    if c2_channels and not consistent:
        reasons.append("C2 多通道到达时间不一致")

    channel_metrics: dict[str, Any] = {}
    good_rir_channels = 0
    for channel in range(rirs.shape[1]):
        raw = recording[:, channel] if recording.size else np.zeros(0)
        rir = np.asarray(rirs[:, channel], dtype=np.float64)
        raw_peak = float(np.max(np.abs(raw))) if raw.size else 0.0
        clipped = int(np.count_nonzero(np.abs(raw) >= 0.999))
        peak = float(np.max(np.abs(rir))) if rir.size else 0.0
        local_start = max(0, direct_index - round(0.002 * SAMPLE_RATE))
        local_stop = min(rir.size, direct_index + round(0.010 * SAMPLE_RATE))
        direct_peak = float(np.max(np.abs(rir[local_start:local_stop]))) if local_stop > local_start else 0.0
        pre = rir[:max(1, local_start)]
        tail = rir[min(rir.size, direct_index + round(0.10 * SAMPLE_RATE)):]
        noise = float(np.sqrt(np.mean(pre * pre))) if pre.size else 0.0
        tail_noise = float(np.sqrt(np.mean(tail * tail))) if tail.size else noise
        confidence_db = 20.0 * np.log10(direct_peak / max(noise, 1e-12)) if direct_peak else None
        peak_tail_db = 20.0 * np.log10(direct_peak / max(tail_noise, 1e-12)) if direct_peak else None
        metrics = _decay_metrics(rir, direct_index)
        channel_pass = (
            direct_peak > 1e-8
            and clipped == 0
            and confidence_db is not None and confidence_db >= 6.0
            and peak_tail_db is not None and peak_tail_db >= 3.0
        )
        if channel_pass:
            good_rir_channels += 1
        channel_metrics[str(channel)] = {
            "raw_peak_fs": raw_peak,
            "clipped_samples": clipped,
            "rir_peak": peak,
            "direct_peak": direct_peak,
            "direct_peak_confidence_db": confidence_db,
            "peak_to_tail_noise_db": peak_tail_db,
            "pass": bool(channel_pass),
            **metrics,
        }
    rir_pass = protocol_pass and consistent and good_rir_channels >= min_channels
    if good_rir_channels < min_channels:
        reasons.append(f"RIR 质量有效通道不足：{good_rir_channels} < {min_channels}")
    tof_pass = bool(tof_available)
    policy_values = {
        "protocol": protocol_pass,
        "rir": protocol_pass and rir_pass,
        "tof": protocol_pass and tof_pass,
        "strict": protocol_pass and rir_pass and tof_pass,
    }
    return {
        "c1_valid_channels": c1_channels,
        "c2_valid_channels": c2_channels,
        "arrival_consistent": consistent,
        "rir_valid_channels": good_rir_channels,
        "channels": channel_metrics,
        "quality_failure_reasons": reasons,
        "protocol_pass": protocol_pass,
        "tof_pass": tof_pass,
        "rir_pass": rir_pass,
        "overall_policy": overall_policy,
        "overall_pass": bool(policy_values[overall_policy]),
        "overall": "PASS" if policy_values[overall_policy] else "FAIL",
    }
