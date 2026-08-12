from __future__ import annotations

from typing import Any

import numpy as np

from .config import SAMPLE_RATE
from .matched_filter import channel_status, detect_multichannel
from .tof import calculate_tof


def analyze_recording(
    recording: np.ndarray,
    c1: np.ndarray,
    c2: np.ndarray,
    *,
    playback_issue_sample: int | None,
    c1_threshold: float,
    c2_threshold: float,
    reply_timeout: float,
    android_messages: list[dict[str, Any]] | None = None,
    speed_of_sound: float = 343.0,
    linux_local_reference_correction: float | None = None,
) -> dict[str, Any]:
    statuses = channel_status(recording)
    if playback_issue_sample is None:
        c1_start, c1_stop = 0, recording.shape[0]
    else:
        # A small lead tolerates callback/output buffering while preventing a
        # previous continuous-session C1 from being reused by the next round.
        c1_start = max(0, playback_issue_sample - round(0.02 * SAMPLE_RATE))
        c1_stop = min(recording.shape[0], playback_issue_sample + c1.size + round(0.5 * SAMPLE_RATE))
    c1_result = detect_multichannel(
        recording, c1, c1_threshold, statuses, start=c1_start, stop=c1_stop
    )
    if c1_result["system_sample"] is None:
        c2_start = playback_issue_sample or 0
    else:
        c2_start = int(c1_result["system_sample"]) + c1.size
    c2_stop = min(
        recording.shape[0],
        (playback_issue_sample or 0) + c1.size + round(reply_timeout * SAMPLE_RATE),
    )
    c2_result = detect_multichannel(
        recording, c2, c2_threshold, statuses, start=c2_start, stop=c2_stop
    )
    t1, t4 = c1_result["system_sample"], c2_result["system_sample"]
    roundtrip_samples = None if t1 is None or t4 is None else int(t4 - t1)
    messages = android_messages or []
    tof = calculate_tof(
        roundtrip_samples, SAMPLE_RATE, messages, speed_of_sound,
        linux_local_reference_correction,
    )
    return {
        "channel_status": statuses,
        "c1_detection": c1_result,
        "c2_detection": c2_result,
        "linux_observed_roundtrip_samples": roundtrip_samples,
        "linux_observed_roundtrip_ms": None if roundtrip_samples is None else roundtrip_samples * 1000.0 / SAMPLE_RATE,
        "tof": tof,
    }
