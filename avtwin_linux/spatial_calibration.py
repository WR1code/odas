from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import struct
from typing import Any

import numpy as np
from scipy.spatial import cKDTree


AVPC_MAGIC = b"AVPC0001"


def _points(value: Any) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if result.ndim != 2 or result.shape[1] != 3:
        raise ValueError("点云必须是 N×3 数组")
    result = result[np.all(np.isfinite(result), axis=1)]
    if result.shape[0] < 3:
        raise ValueError("点云有效点不足")
    return result


def write_avpc(path: Path, points: Any, metadata: dict[str, Any] | None = None) -> None:
    """Write a compact, versioned XYZ point cloud shared by iOS and Linux."""
    values = np.asarray(_points(points), dtype="<f4")
    header = {
        "format": "AVTWIN_POINT_CLOUD_V1",
        "point_count": int(values.shape[0]),
        "fields": ["x", "y", "z"],
        "dtype": "float32_le",
        "unit": "metre",
        **(metadata or {}),
    }
    encoded = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(AVPC_MAGIC)
        stream.write(struct.pack("<I", len(encoded)))
        stream.write(encoded)
        stream.write(values.tobytes(order="C"))


def read_avpc(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    with path.open("rb") as stream:
        if stream.read(len(AVPC_MAGIC)) != AVPC_MAGIC:
            raise ValueError("不是 AVTWIN_POINT_CLOUD_V1 文件")
        length_bytes = stream.read(4)
        if len(length_bytes) != 4:
            raise ValueError("点云头损坏")
        header_length = struct.unpack("<I", length_bytes)[0]
        if header_length <= 0 or header_length > 1_048_576:
            raise ValueError("点云头长度无效")
        header = json.loads(stream.read(header_length).decode("utf-8"))
        if header.get("format") != "AVTWIN_POINT_CLOUD_V1":
            raise ValueError("不支持的点云格式版本")
        count = int(header.get("point_count", -1))
        payload = stream.read()
    if count < 0 or len(payload) != count * 3 * 4:
        raise ValueError("点云数据长度与 point_count 不一致")
    values = np.frombuffer(payload, dtype="<f4").reshape(count, 3).astype(np.float64)
    return _points(values), header


def voxel_downsample(points: Any, voxel_size_m: float) -> np.ndarray:
    values = _points(points)
    if not math.isfinite(voxel_size_m) or voxel_size_m <= 0:
        raise ValueError("体素尺寸必须为正数")
    keys = np.floor(values / voxel_size_m).astype(np.int64)
    _, first = np.unique(keys, axis=0, return_index=True)
    return values[np.sort(first)]


def apply_transform(points: Any, matrix: Any) -> np.ndarray:
    values = _points(points)
    transform = np.asarray(matrix, dtype=np.float64)
    if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
        raise ValueError("刚体变换必须是有限的 4×4 矩阵")
    return values @ transform[:3, :3].T + transform[:3, 3]


def _yaw_matrix(angle: float) -> np.ndarray:
    cosine, sine = math.cos(angle), math.sin(angle)
    return np.array([
        [cosine, -sine, 0.0], [sine, cosine, 0.0], [0.0, 0.0, 1.0],
    ])


def _compose(rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = rotation
    result[:3, 3] = translation
    return result


def _fit_gravity_aligned(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    source_center = np.mean(source, axis=0)
    target_center = np.mean(target, axis=0)
    first = source[:, :2] - source_center[:2]
    second = target[:, :2] - target_center[:2]
    dot = float(np.sum(first[:, 0] * second[:, 0] + first[:, 1] * second[:, 1]))
    cross = float(np.sum(first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]))
    rotation = _yaw_matrix(math.atan2(cross, dot))
    translation = target_center - rotation @ source_center
    return _compose(rotation, translation)


def _icp(
    source: np.ndarray,
    target: np.ndarray,
    initial: np.ndarray,
    *,
    correspondence_m: float,
    iterations: int,
) -> tuple[np.ndarray, np.ndarray]:
    tree = cKDTree(target)
    transform = initial.copy()
    distances = np.empty(0)
    for _ in range(iterations):
        moved = apply_transform(source, transform)
        distances, indices = tree.query(moved, workers=-1)
        usable = np.isfinite(distances) & (distances <= correspondence_m)
        if np.count_nonzero(usable) < 12:
            break
        usable_indices = np.flatnonzero(usable)
        # Trim the least consistent tail; the phone normally observes only a
        # subset of the stationary 360-degree LiDAR map.
        keep_count = max(12, round(usable_indices.size * 0.75))
        chosen = usable_indices[np.argpartition(distances[usable_indices], keep_count - 1)[:keep_count]]
        update = _fit_gravity_aligned(moved[chosen], target[indices[chosen]])
        transform = update @ transform
        yaw_step = math.atan2(update[1, 0], update[0, 0])
        if np.linalg.norm(update[:3, 3]) < 1e-4 and abs(yaw_step) < 1e-4:
            break
    moved = apply_transform(source, transform)
    distances, _ = tree.query(moved, workers=-1)
    return transform, distances


@dataclass(slots=True)
class CalibrationQuality:
    accepted: bool
    rmse_m: float
    median_error_m: float
    p95_error_m: float
    inlier_ratio: float
    inlier_threshold_m: float
    source_points: int
    target_points: int
    ambiguity_ratio: float
    reason: str


@dataclass(slots=True)
class CalibrationResult:
    protocol: str
    source_frame_id: str
    target_frame_id: str
    target_from_source: list[list[float]]
    quality: CalibrationQuality
    method: str = "gravity_aligned_multistart_trimmed_icp"
    scale_fixed: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["transform_convention"] = "p_target = R_target_from_source * p_source + t_target_from_source"
        return value


def calibrate_point_clouds(
    source_points: Any,
    target_points: Any,
    *,
    source_frame_id: str = "arkit_user_origin_x_forward_y_left_z_up",
    target_frame_id: str = "mid360_map",
    voxel_size_m: float = 0.12,
    inlier_threshold_m: float = 0.25,
    yaw_step_degrees: float = 15.0,
    iterations: int = 35,
) -> CalibrationResult:
    """Register a gravity-aligned phone map into a stationary LiDAR map.

    ARKit and FAST-LIO both supply metric scale and gravity, so calibration is
    deliberately constrained to yaw plus XYZ translation. This prevents a
    geometrically weak wall/floor match from inventing roll, pitch, or scale.
    """
    source = voxel_downsample(source_points, voxel_size_m)
    target = voxel_downsample(target_points, voxel_size_m)
    if source.shape[0] < 80 or target.shape[0] < 80:
        raise ValueError("配准至少需要两边各 80 个体素点")
    if yaw_step_degrees <= 0 or yaw_step_degrees > 90:
        raise ValueError("yaw 搜索步长必须在 (0, 90] 度")
    source_center = np.median(source, axis=0)
    target_center = np.median(target, axis=0)
    candidates: list[tuple[float, np.ndarray, np.ndarray]] = []
    for yaw_degrees in np.arange(-180.0, 180.0, yaw_step_degrees):
        rotation = _yaw_matrix(math.radians(float(yaw_degrees)))
        initial = _compose(rotation, target_center - rotation @ source_center)
        transform, distances = _icp(
            source, target, initial,
            correspondence_m=max(1.0, inlier_threshold_m * 4.0),
            iterations=iterations,
        )
        transform, distances = _icp(
            source, target, transform,
            correspondence_m=max(inlier_threshold_m * 1.6, voxel_size_m * 3.0),
            iterations=iterations,
        )
        inliers = distances <= inlier_threshold_m
        if np.any(inliers):
            score = float(np.mean(np.minimum(distances, inlier_threshold_m) ** 2))
        else:
            score = math.inf
        candidates.append((score, transform, distances))
    candidates.sort(key=lambda value: value[0])
    score, transform, distances = candidates[0]
    inliers = distances <= inlier_threshold_m
    inlier_distances = distances[inliers]
    inlier_ratio = float(np.mean(inliers))
    rmse = float(np.sqrt(np.mean(inlier_distances ** 2))) if inlier_distances.size else math.inf
    median = float(np.median(inlier_distances)) if inlier_distances.size else math.inf
    p95 = float(np.percentile(inlier_distances, 95)) if inlier_distances.size else math.inf
    best_yaw = math.atan2(transform[1, 0], transform[0, 0])
    second_score = math.inf
    for candidate_score, candidate_transform, _candidate_distances in candidates[1:]:
        candidate_yaw = math.atan2(candidate_transform[1, 0], candidate_transform[0, 0])
        yaw_delta = abs(math.atan2(
            math.sin(candidate_yaw - best_yaw), math.cos(candidate_yaw - best_yaw),
        ))
        translation_delta = float(np.linalg.norm(candidate_transform[:3, 3] - transform[:3, 3]))
        # Several coarse seeds commonly converge to the same ICP basin. They
        # are one solution, not evidence of scene ambiguity.
        if yaw_delta >= math.radians(5.0) or translation_delta >= 0.10:
            second_score = candidate_score
            break
    ambiguity_ratio = float(second_score / max(score, 1e-12)) if math.isfinite(second_score) else math.inf
    accepted = bool(inlier_ratio >= 0.45 and rmse <= inlier_threshold_m * 0.65 and ambiguity_ratio >= 1.005)
    reasons = []
    if inlier_ratio < 0.45:
        reasons.append(f"重叠内点率不足 ({inlier_ratio:.1%} < 45%)")
    if rmse > inlier_threshold_m * 0.65:
        reasons.append(f"内点 RMSE 过大 ({rmse:.3f}m)")
    if ambiguity_ratio < 1.005:
        reasons.append("存在近似等价的旋转解，场景可能对称")
    quality = CalibrationQuality(
        accepted=accepted,
        rmse_m=rmse,
        median_error_m=median,
        p95_error_m=p95,
        inlier_ratio=inlier_ratio,
        inlier_threshold_m=inlier_threshold_m,
        source_points=int(source.shape[0]),
        target_points=int(target.shape[0]),
        ambiguity_ratio=ambiguity_ratio,
        reason="通过" if accepted else "；".join(reasons),
    )
    return CalibrationResult(
        protocol="AVTWIN_SPATIAL_CALIBRATION_V1",
        source_frame_id=source_frame_id,
        target_frame_id=target_frame_id,
        target_from_source=transform.tolist(),
        quality=quality,
    )


def save_calibration(path: Path, result: CalibrationResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_calibration(path: Path, *, require_accepted: bool = True) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("protocol") != "AVTWIN_SPATIAL_CALIBRATION_V1":
        raise ValueError("不支持的空间标定文件")
    matrix = np.asarray(value.get("target_from_source"), dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError("空间标定矩阵无效")
    if require_accepted and not bool((value.get("quality") or {}).get("accepted")):
        raise ValueError("空间标定质量未通过，不能应用")
    return value
