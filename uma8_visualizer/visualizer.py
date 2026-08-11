"""matplotlib 极坐标实时界面。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from matplotlib import pyplot as plt
from matplotlib.animation import FuncAnimation

from .aoa_math import DirectionSmoother, angle_degrees
from .odas_process import ODASReader
from .track_selector import Track, TrackSelector


class AoAVisualizer:
    def __init__(self, reader: ODASReader, selector: TrackSelector, *, no_source_timeout: float,
                 smoothing_alpha: float, angle_offset: float, max_visible_tracks: int,
                 on_close: Callable[[], None]) -> None:
        self.reader = reader
        self.selector = selector
        self.no_source_timeout = no_source_timeout
        self.angle_offset = angle_offset
        self.max_visible_tracks = max_visible_tracks
        self.on_close = on_close
        self.smoother = DirectionSmoother(smoothing_alpha)
        self.last_active = 0.0
        self.last_track: Track | None = None
        self._closed = False

        # 沿用原型中更紧凑的正方形方向盘布局。
        self.fig, self.ax = plt.subplots(figsize=(7.2, 7.2), subplot_kw={"projection": "polar"})
        self.fig.canvas.manager.set_window_title("UMA-8 v2 Real-time Angle of Arrival")
        self.ax.set_theta_zero_location("N")
        self.ax.set_theta_direction(1)
        self.ax.set_ylim(0.0, 1.0)
        self.ax.set_yticks([])
        labels = [f"{d}°" for d in range(0, 360, 30)]
        labels[0], labels[3], labels[6], labels[9] = "0° Front", "90° Left", "180° Rear / USB", "270° Right"
        self.ax.set_thetagrids(range(0, 360, 30), labels=labels)
        self.ax.set_title("UMA-8 v2 — Real-time Angle of Arrival", pad=25)
        (self.main_line,) = self.ax.plot([], [], linewidth=4.0, color="#d64b32", zorder=4)
        (self.main_point,) = self.ax.plot([], [], marker="o", markersize=10, color="#d64b32", zorder=5)
        self.secondary_lines = []
        self.secondary_points = []
        for _ in range(max_visible_tracks):
            (line,) = self.ax.plot([], [], linewidth=1.5, alpha=0.25, color="#2878b5", zorder=2)
            (point,) = self.ax.plot([], [], marker="o", markersize=5, alpha=0.35,
                                    color="#2878b5", zorder=3)
            self.secondary_lines.append(line)
            self.secondary_points.append(point)
        self.info = self.fig.text(
            0.5, 0.055,
            "Waiting for an active sound source...\n"
            "0° Front · 90° Left · 180° Rear · 270° Right",
            ha="center", va="center",
        )
        self.fig.subplots_adjust(bottom=0.18)
        self.fig.canvas.mpl_connect("close_event", self._close)
        self.animation = FuncAnimation(self.fig, self._update, interval=50, cache_frame_data=False)

    def _set_line(self, line: object, angle_deg: float, radius: float = 1.0) -> None:
        theta = math.radians(angle_deg % 360.0)
        line.set_data([theta, theta], [0.0, radius])  # type: ignore[attr-defined]

    def _update(self, _frame: int) -> tuple[object, ...]:
        message = self.reader.latest()
        tracks: list[Track] = []
        if message is not None:
            tracks = self.selector.valid_tracks(message)
            selected = self.selector.select(tracks)
            if selected is not None:
                if self.last_track is None or selected.track_id != self.last_track.track_id:
                    self.smoother.reset()
                self.last_track = selected
                self.last_active = time.monotonic()
                sx, sy = self.smoother.update(selected.x, selected.y)
                shown_angle = (angle_degrees(sx, sy) + self.angle_offset) % 360.0
                self._set_line(self.main_line, shown_angle)
                self.main_point.set_data([math.radians(shown_angle)], [1.0])
                self.info.set_text(
                    f"AoA: {shown_angle:6.1f}°    Track ID: {selected.track_id}    Activity: {selected.activity:.3f}\n"
                    f"x={selected.x:+.3f}    y={selected.y:+.3f}    z={selected.z:+.3f}    Active tracks: {len(tracks)}"
                )

        if time.monotonic() - self.last_active > self.no_source_timeout:
            self.main_line.set_data([], [])
            self.main_point.set_data([], [])
            self.last_track = None
            self.selector.current_track_id = None
            self.smoother.reset()
            self.info.set_text(
                "Waiting for an active sound source...\n"
                "0° Front · 90° Left · 180° Rear · 270° Right"
            )

        # 原型界面的候选轨迹：按 activity 排序，线长直接表达活动度。
        visible_tracks = sorted(tracks, key=lambda track: track.activity, reverse=True)[: self.max_visible_tracks]
        for line, point, track in zip(self.secondary_lines, self.secondary_points, visible_tracks):
            shown_angle = (angle_degrees(track.x, track.y) + self.angle_offset) % 360.0
            radius = 0.35 + 0.55 * max(0.0, min(track.activity, 1.0))
            self._set_line(line, shown_angle, radius)
            point.set_data([math.radians(shown_angle)], [radius])
        for line in self.secondary_lines[len(visible_tracks):]:
            line.set_data([], [])
        for point in self.secondary_points[len(visible_tracks):]:
            point.set_data([], [])
        if self.reader.error:
            self.info.set_text(self.reader.error)
        return (
            self.main_line, self.main_point, self.info,
            *self.secondary_lines, *self.secondary_points,
        )

    def _close(self, _event: object) -> None:
        if not self._closed:
            self._closed = True
            self.on_close()

    def show(self) -> None:
        try:
            plt.show()
        finally:
            self._close(None)
