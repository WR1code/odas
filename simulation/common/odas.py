"""ODAS concatenated-JSON parsing and direction evaluation."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterator

from .geometry import angular_error_deg


def iter_json_objects(text: str) -> Iterator[dict]:
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            return
        value, index = decoder.raw_decode(text, index)
        yield value


def load_track_frames(path: str | Path) -> list[dict]:
    return list(iter_json_objects(Path(path).read_text(encoding="utf-8")))


def active_directions(frames: list[dict], activity_min: float = 0.2) -> list[dict]:
    result = []
    for frame in frames:
        for source in frame.get("src", []):
            if source.get("id", 0) and source.get("activity", 0.0) >= activity_min:
                x, y, z = source["x"], source["y"], source["z"]
                result.append({
                    **source,
                    "timeStamp": frame.get("timeStamp"),
                    "azimuth_deg": math.degrees(math.atan2(y, x)),
                    "elevation_deg": math.degrees(math.atan2(z, math.hypot(x, y))),
                })
    return result


def evaluate_tracks(path: str | Path, truth: dict[str, float]) -> dict:
    frames = load_track_frames(path)
    directions = []
    for frame in frames:
        candidates = [item for item in frame.get("src", [])
                      if item.get("id", 0) and item.get("activity", 0.0) >= 0.2]
        if not candidates:
            continue
        source = max(candidates, key=lambda item: item.get("activity", 0.0))
        x, y, z = source["x"], source["y"], source["z"]
        directions.append({**source, "timeStamp": frame.get("timeStamp"),
                           "azimuth_deg": math.degrees(math.atan2(y, x)),
                           "elevation_deg": math.degrees(math.atan2(z, math.hypot(x, y)))})
    if not directions:
        return {"frames_total": len(frames), "detections": 0, "detection_rate": 0.0,
                "azimuth_mae_deg": None, "elevation_mae_deg": None}
    az_errors = [angular_error_deg(item["azimuth_deg"], truth["azimuth_deg"]) for item in directions]
    el_errors = [abs(item["elevation_deg"] - truth["elevation_deg"]) for item in directions]
    az_sorted = sorted(az_errors)
    el_sorted = sorted(el_errors)
    return {
        "frames_total": len(frames),
        "detections": len(directions),
        "detection_rate": len(directions) / len(frames) if frames else 0.0,
        "azimuth_mae_deg": sum(az_errors) / len(az_errors),
        "azimuth_median_error_deg": az_sorted[len(az_sorted) // 2],
        "azimuth_p95_error_deg": az_sorted[min(len(az_sorted) - 1, int(0.95 * len(az_sorted)))],
        "elevation_mae_deg": sum(el_errors) / len(el_errors),
        "elevation_median_error_deg": el_sorted[len(el_sorted) // 2],
        "elevation_p95_error_deg": el_sorted[min(len(el_sorted) - 1, int(0.95 * len(el_sorted)))],
        "last_direction": directions[-1],
        "distance_error_m": None,
        "distance_note": "ODAS SST emits unit directions, not range",
    }
