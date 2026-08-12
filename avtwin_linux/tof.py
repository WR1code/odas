from __future__ import annotations

from typing import Any
import math


def calculate_tof(
    roundtrip_samples: int | None,
    sample_rate: int,
    android_messages: list[dict[str, Any]],
    speed_of_sound: float,
    linux_local_reference_correction: float | None,
) -> dict[str, Any]:
    precise = next((m for m in reversed(android_messages) if m.get("t3_precise") is True), None)
    if roundtrip_samples is None:
        return {"available": False, "reason": "C1/C2 acoustic detections are incomplete"}
    if precise is None:
        return {
            "available": False,
            "reason": "Android precise reply delay is not available",
            "exact_tof": "NOT AVAILABLE",
        }
    try:
        if "reply_delay_ns" in precise:
            reply_delay_value = float(precise["reply_delay_ns"])
            reply_delay_s = reply_delay_value / 1e9
            source = "reply_delay_ns"
        elif "reply_delay_samples" in precise:
            android_rate = float(precise.get("sample_rate", sample_rate))
            if not math.isfinite(android_rate) or android_rate <= 0:
                return {"available": False, "reason": "Android reply sample rate is invalid", "exact_tof": "NOT AVAILABLE"}
            reply_delay_s = float(precise["reply_delay_samples"]) / android_rate
            source = "reply_delay_samples"
        else:
            return {"available": False, "reason": "Android t3_precise=true but reply delay is missing"}
    except (TypeError, ValueError, OverflowError):
        return {"available": False, "reason": "Android precise reply delay is invalid", "exact_tof": "NOT AVAILABLE"}
    if not math.isfinite(reply_delay_s) or reply_delay_s < 0:
        return {"available": False, "reason": "Android precise reply delay is invalid", "exact_tof": "NOT AVAILABLE"}
    correction = linux_local_reference_correction or 0.0
    corrected = roundtrip_samples / sample_rate - reply_delay_s - correction
    if corrected < 0:
        return {"available": False, "reason": "Corrected round-trip time is negative"}
    tof_s = corrected / 2.0
    return {
        "available": True,
        "tof_seconds": tof_s,
        "distance_m": tof_s * speed_of_sound,
        "android_reply_delay_seconds": reply_delay_s,
        "android_reply_delay_source": source,
        "linux_local_reference_correction_seconds": linux_local_reference_correction,
        "calibration": "calibrated" if linux_local_reference_correction is not None else "preliminary / uncalibrated",
    }
