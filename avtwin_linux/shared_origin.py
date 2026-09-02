from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

import numpy as np

from .pose import parse_vector3


SHARED_ORIGIN_MODES = {"linux_microphone", "iphone_current"}


def _quaternion(value: Any) -> np.ndarray:
    quaternion = np.asarray(value, dtype=np.float64)
    if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
        raise ValueError("姿态四元数必须是有限的 xyzw 四元数")
    length = float(np.linalg.norm(quaternion))
    if length <= 1e-9:
        raise ValueError("姿态四元数长度不能为零")
    return quaternion / length


def pose_matrix(pose: dict[str, Any]) -> np.ndarray:
    position = np.asarray(parse_vector3(pose.get("position_m", ())), dtype=np.float64)
    x, y, z, w = _quaternion(pose.get("orientation_xyzw", ()))
    rotation = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = rotation
    result[:3, 3] = position
    return result


def matrix_pose(matrix: Any, *, frame_id: str, child_frame_id: str) -> dict[str, Any]:
    transform = np.asarray(matrix, dtype=np.float64)
    if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
        raise ValueError("位姿矩阵必须是有限的 4×4 矩阵")
    rotation = transform[:3, :3]
    trace = float(np.trace(rotation))
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2
        quaternion = np.array([
            (rotation[2, 1] - rotation[1, 2]) / scale,
            (rotation[0, 2] - rotation[2, 0]) / scale,
            (rotation[1, 0] - rotation[0, 1]) / scale,
            0.25 * scale,
        ])
    else:
        index = int(np.argmax(np.diag(rotation)))
        if index == 0:
            scale = math.sqrt(1 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2]) * 2
            quaternion = np.array([
                0.25 * scale,
                (rotation[0, 1] + rotation[1, 0]) / scale,
                (rotation[0, 2] + rotation[2, 0]) / scale,
                (rotation[2, 1] - rotation[1, 2]) / scale,
            ])
        elif index == 1:
            scale = math.sqrt(1 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2]) * 2
            quaternion = np.array([
                (rotation[0, 1] + rotation[1, 0]) / scale,
                0.25 * scale,
                (rotation[1, 2] + rotation[2, 1]) / scale,
                (rotation[0, 2] - rotation[2, 0]) / scale,
            ])
        else:
            scale = math.sqrt(1 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1]) * 2
            quaternion = np.array([
                (rotation[0, 2] + rotation[2, 0]) / scale,
                (rotation[1, 2] + rotation[2, 1]) / scale,
                0.25 * scale,
                (rotation[1, 0] - rotation[0, 1]) / scale,
            ])
    quaternion /= np.linalg.norm(quaternion)
    return {
        "position_m": transform[:3, 3].tolist(),
        "orientation_xyzw": quaternion.tolist(),
        "frame_id": frame_id,
        "child_frame_id": child_frame_id,
    }


def configure_shared_origin(
    calibration: dict[str, Any],
    *,
    mode: str,
    phone_origin_pose: dict[str, Any],
    linux_microphone_pose: dict[str, Any],
    phone_source_to_world: Any | None = None,
) -> dict[str, Any]:
    """Create one shared frame without modifying the underlying LiDAR map.

    ``phone_origin_pose`` is the phone's current position plus its new
    gravity-aligned origin orientation, expressed in the phone's current
    source frame.  ``linux_microphone_pose`` is expressed in the raw target
    frame of the accepted map calibration.
    """
    normalized_mode = mode.strip().lower()
    if normalized_mode not in SHARED_ORIGIN_MODES:
        raise ValueError(f"不支持的共享原点模式：{mode}")
    if calibration.get("protocol") != "AVTWIN_SPATIAL_CALIBRATION_V1":
        raise ValueError("不支持的手机/雷达空间标定协议")
    if not bool((calibration.get("quality") or {}).get("accepted")):
        raise ValueError("手机/雷达空间标定尚未通过")
    target_frame = str(calibration.get("target_frame_id") or "")
    source_frame = str(calibration.get("source_frame_id") or "")
    if str(phone_origin_pose.get("frame_id") or "") != source_frame:
        raise ValueError("iPhone 当前位姿与标定源 frame 不一致")
    if str(linux_microphone_pose.get("frame_id") or "") != target_frame:
        raise ValueError("Linux 麦克风位姿与雷达标定目标 frame 不一致")

    world_from_source = np.asarray(
        calibration.get("target_from_source")
        if phone_source_to_world is None else phone_source_to_world,
        dtype=np.float64,
    )
    if world_from_source.shape != (4, 4) or not np.all(np.isfinite(world_from_source)):
        raise ValueError("手机当前源坐标到雷达坐标的变换无效")
    world_from_phone_current = world_from_source @ pose_matrix(phone_origin_pose)
    world_from_microphone = pose_matrix(linux_microphone_pose)

    if normalized_mode == "linux_microphone":
        shared_frame_id = f"{target_frame}/linux_microphone_origin"
        world_from_shared = world_from_microphone
        shared_from_phone_source = np.linalg.inv(world_from_shared) @ world_from_source
        next_phone_source_to_world = world_from_source
        phone_reset_required = False
    else:
        shared_frame_id = f"{target_frame}/iphone_current_origin"
        world_from_shared = world_from_phone_current
        # After the ACK, iOS resets its numeric/visual origin to precisely the
        # pose sent in this request, so its new source frame is the shared frame.
        shared_from_phone_source = np.eye(4, dtype=np.float64)
        next_phone_source_to_world = world_from_phone_current
        phone_reset_required = True

    shared_from_world = np.linalg.inv(world_from_shared)
    shared_from_phone_current = shared_from_world @ world_from_phone_current
    shared_from_microphone = shared_from_world @ world_from_microphone
    phone_source_from_shared = np.linalg.inv(shared_from_phone_source)
    phone_source_from_linux_microphone = phone_source_from_shared @ shared_from_microphone
    derived_calibration = deepcopy(calibration)
    derived_calibration.update({
        "source_frame_id": source_frame or "arkit_user_origin_x_forward_y_left_z_up",
        "target_frame_id": shared_frame_id,
        "target_from_source": shared_from_phone_source.tolist(),
        "derived_from_target_frame_id": target_frame,
        "shared_origin_mode": normalized_mode,
        "transform_convention": "p_target = R_target_from_source * p_source + t_target_from_source",
    })
    return {
        "mode": normalized_mode,
        "shared_frame_id": shared_frame_id,
        "phone_reset_required": phone_reset_required,
        "world_from_shared_origin": world_from_shared.tolist(),
        "next_phone_source_to_world": next_phone_source_to_world.tolist(),
        "shared_from_phone_source": shared_from_phone_source.tolist(),
        "phone_source_from_shared_origin": phone_source_from_shared.tolist(),
        "phone_source_from_linux_microphone": phone_source_from_linux_microphone.tolist(),
        "phone_position_m": shared_from_phone_current[:3, 3].tolist(),
        "linux_microphone_position_m": shared_from_microphone[:3, 3].tolist(),
        "origin_pose_world": matrix_pose(
            world_from_shared, frame_id=target_frame, child_frame_id="shared_origin"
        ),
        "derived_calibration": derived_calibration,
    }
