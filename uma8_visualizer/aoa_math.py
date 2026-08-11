"""二维到达角计算与圆周方向平滑。"""

from __future__ import annotations

import math


def angle_degrees(x: float, y: float) -> float:
    """返回 [0, 360) 角度：+X 为 0°，+Y 为 90°。"""
    return math.degrees(math.atan2(y, x)) % 360.0


def calibrated_angle(x: float, y: float, offset_degrees: float = 0.0) -> float:
    return (angle_degrees(x, y) + offset_degrees) % 360.0


class DirectionSmoother:
    """在单位方向向量上做 EMA，避免 359°/1° 被平均为 180°。"""

    def __init__(self, alpha: float = 0.22) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha 必须在 (0, 1] 内")
        self.alpha = alpha
        self._x: float | None = None
        self._y: float | None = None

    def reset(self) -> None:
        self._x = self._y = None

    def update(self, x: float, y: float) -> tuple[float, float]:
        length = math.hypot(x, y)
        if length <= 0.0:
            raise ValueError("方向向量不能为零")
        unit_x, unit_y = x / length, y / length
        if self._x is None or self._y is None:
            mixed_x, mixed_y = unit_x, unit_y
        else:
            mixed_x = (1.0 - self.alpha) * self._x + self.alpha * unit_x
            mixed_y = (1.0 - self.alpha) * self._y + self.alpha * unit_y
        mixed_length = math.hypot(mixed_x, mixed_y)
        if mixed_length < 1e-12:
            mixed_x, mixed_y, mixed_length = unit_x, unit_y, 1.0
        self._x, self._y = mixed_x / mixed_length, mixed_y / mixed_length
        return self._x, self._y

    @property
    def angle(self) -> float | None:
        if self._x is None or self._y is None:
            return None
        return angle_degrees(self._x, self._y)
