"""ODAS 声源过滤与主 Track 稳定选择。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Track:
    track_id: int
    x: float
    y: float
    z: float
    activity: float

    @property
    def horizontal(self) -> float:
        return math.hypot(self.x, self.y)

    @property
    def score(self) -> float:
        # 优先 activity 和水平可信度，大 Z 分量作温和降权。
        z_penalty = 1.0 / (1.0 + abs(self.z))
        return self.activity * min(self.horizontal, 1.0) * z_penalty


class TrackSelector:
    def __init__(self, activity_threshold: float = 0.05, hold_threshold: float = 0.20) -> None:
        self.activity_threshold = activity_threshold
        self.hold_threshold = hold_threshold
        self.current_track_id: int | None = None

    def valid_tracks(self, message: dict[str, Any]) -> list[Track]:
        tracks: list[Track] = []
        sources = message.get("src", [])
        if not isinstance(sources, list):
            return tracks
        for source in sources:
            if not isinstance(source, dict):
                continue
            try:
                track = Track(
                    track_id=int(source["id"]),
                    x=float(source["x"]),
                    y=float(source["y"]),
                    z=float(source["z"]),
                    activity=float(source["activity"]),
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            values = (track.x, track.y, track.z, track.activity)
            if not all(math.isfinite(value) for value in values):
                continue
            if track.track_id != 0 and track.activity >= self.activity_threshold and track.horizontal > 0.05:
                tracks.append(track)
        return sorted(tracks, key=lambda item: item.score, reverse=True)

    def select(self, tracks: list[Track]) -> Track | None:
        current = next((t for t in tracks if t.track_id == self.current_track_id), None)
        if current is not None and current.activity >= self.hold_threshold:
            return current
        selected = tracks[0] if tracks else None
        self.current_track_id = selected.track_id if selected else None
        return selected
