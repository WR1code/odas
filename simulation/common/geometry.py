"""Coordinate transforms and the verified seven-microphone UMA-8 geometry.

The integration uses a right-handed, Z-up metric frame everywhere.  Array +X is
forward, +Y is left, and +Z is up.  Azimuth is atan2(y, x): zero is forward and
positive angles turn counter-clockwise when viewed from above.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np


UMA8_ACTIVE_MICS_M = np.asarray(
    [
        [0.0000, 0.0000, 0.0000],
        [0.0000, 0.0422, 0.0000],
        [-0.0366, 0.0211, 0.0000],
        [-0.0366, -0.0211, 0.0000],
        [0.0000, -0.0422, 0.0000],
        [0.0366, -0.0211, 0.0000],
        [0.0366, 0.0211, 0.0000],
    ],
    dtype=np.float64,
)


@dataclass(frozen=True)
class Pose:
    translation_m: np.ndarray
    quaternion_wxyz: np.ndarray

    @classmethod
    def from_values(cls, translation_m: Iterable[float], quaternion_wxyz: Iterable[float]) -> "Pose":
        t = np.asarray(tuple(translation_m), dtype=np.float64)
        q = np.asarray(tuple(quaternion_wxyz), dtype=np.float64)
        if t.shape != (3,) or q.shape != (4,):
            raise ValueError("pose requires translation[3] and quaternion wxyz[4]")
        norm = np.linalg.norm(q)
        if norm == 0:
            raise ValueError("zero quaternion")
        return cls(t, q / norm)


def quaternion_matrix_wxyz(q: Iterable[float]) -> np.ndarray:
    w, x, y, z = np.asarray(tuple(q), dtype=np.float64)
    n = math.sqrt(w*w + x*x + y*y + z*z)
    if n == 0:
        raise ValueError("zero quaternion")
    w, x, y, z = w/n, x/n, y/n, z/n
    return np.asarray([
        [1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
        [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
        [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)],
    ])


def transform_points(pose: Pose, local_points_m: np.ndarray) -> np.ndarray:
    points = np.asarray(local_points_m, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("points must have shape [N,3]")
    return points @ quaternion_matrix_wxyz(pose.quaternion_wxyz).T + pose.translation_m


def direction_in_array(source_world_m: Iterable[float], array_pose: Pose) -> np.ndarray:
    delta_world = np.asarray(tuple(source_world_m), dtype=np.float64) - array_pose.translation_m
    return quaternion_matrix_wxyz(array_pose.quaternion_wxyz).T @ delta_world


def spherical_from_vector(vector_xyz: Iterable[float]) -> dict[str, float]:
    x, y, z = np.asarray(tuple(vector_xyz), dtype=np.float64)
    distance = float(np.linalg.norm([x, y, z]))
    if distance == 0:
        raise ValueError("source and array origins coincide")
    return {
        "azimuth_deg": math.degrees(math.atan2(y, x)),
        "elevation_deg": math.degrees(math.atan2(z, math.hypot(x, y))),
        "distance_m": distance,
    }


def angular_error_deg(a_deg: float, b_deg: float) -> float:
    return abs((a_deg - b_deg + 180.0) % 360.0 - 180.0)
