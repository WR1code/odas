from __future__ import annotations

from bisect import bisect_left
from collections import deque
from dataclasses import asdict, dataclass
import json
import math
import socket
import threading
import time
from typing import Any, Protocol

import numpy as np


TRACKING_STATES = {"TRACKING", "OK", "VALID", "MANUAL"}


class AudioSampleClock:
    """Approximate PCM sample -> CLOCK_MONOTONIC mapping from input block delivery."""

    def __init__(self, sample_rate: int, capacity: int = 4096):
        self.sample_rate = sample_rate
        self._anchors: deque[tuple[int, int]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add_anchor(self, end_sample: int, received_monotonic_ns: int | None = None) -> None:
        with self._lock:
            self._anchors.append((int(end_sample), received_monotonic_ns or time.monotonic_ns()))

    def timestamp(self, sample: int) -> tuple[int | None, dict[str, Any]]:
        with self._lock:
            anchors = list(self._anchors)
        if not anchors:
            return None, {"available": False, "reason": "audio clock has no block anchors"}
        anchor_sample, anchor_ns = min(anchors, key=lambda item: abs(item[0] - sample))
        offset_frames = int(sample) - anchor_sample
        timestamp_ns = anchor_ns + round(offset_frames * 1e9 / self.sample_rate)
        return timestamp_ns, {
            "available": True,
            "basis": "CLOCK_MONOTONIC estimated from audio block receipt",
            "anchor_sample": anchor_sample,
            "anchor_monotonic_ns": anchor_ns,
            "offset_frames": offset_frames,
            "nominal_sample_rate": self.sample_rate,
            "hardware_timestamp": False,
            "warning": "callback/ALSA delivery latency is not yet hardware timestamp calibrated",
        }


def parse_vector3(value: str | tuple[float, float, float] | list[float]) -> tuple[float, float, float]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    else:
        parts = list(value)
    if len(parts) != 3:
        raise ValueError("外参必须包含三个数，格式为 x,y,z（单位：米）")
    try:
        result = tuple(float(part) for part in parts)
    except (TypeError, ValueError) as exc:
        raise ValueError("外参必须是三个有限数字，格式为 x,y,z（单位：米）") from exc
    if not all(math.isfinite(item) for item in result):
        raise ValueError("外参不能包含 NaN 或无穷值")
    return result  # type: ignore[return-value]


def _normalize_quaternion(value: Any) -> tuple[float, float, float, float]:
    try:
        quaternion = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("orientation_xyzw 必须是四个数字") from exc
    if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
        raise ValueError("orientation_xyzw 必须是四个有限数字")
    norm = float(np.linalg.norm(quaternion))
    if norm < 1e-9:
        raise ValueError("orientation_xyzw 不能是零四元数")
    return tuple((quaternion / norm).tolist())  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class PoseSample:
    timestamp_ns: int
    position_m: tuple[float, float, float]
    orientation_xyzw: tuple[float, float, float, float]
    frame_id: str = "map"
    child_frame_id: str = "livox_frame"
    tracking_status: str = "TRACKING"
    timestamp_basis: str = "monotonic_ns"
    source: str = "external"

    @classmethod
    def from_message(cls, message: dict[str, Any], received_ns: int) -> "PoseSample":
        if message.get("type") not in {None, "lidar_pose"}:
            raise ValueError("pose message type 必须是 lidar_pose")
        basis = str(message.get("timestamp_basis", "udp_receive_monotonic_ns"))
        supplied = message.get("timestamp_ns")
        if supplied is not None and basis != "monotonic_ns":
            raise ValueError("外部 timestamp_ns 只有标明 monotonic_ns 才能与音频安全对齐")
        timestamp_ns = received_ns if supplied is None else int(supplied)
        if timestamp_ns <= 0:
            raise ValueError("timestamp_ns 必须为正数")
        position = parse_vector3(message.get("position_m", ()))
        orientation = _normalize_quaternion(message.get("orientation_xyzw", ()))
        return cls(
            timestamp_ns=timestamp_ns,
            position_m=position,
            orientation_xyzw=orientation,
            frame_id=str(message.get("frame_id", "map")),
            child_frame_id=str(message.get("child_frame_id", "livox_frame")),
            tracking_status=str(message.get("tracking_status", "TRACKING")).upper(),
            timestamp_basis="monotonic_ns" if supplied is not None else "udp_receive_monotonic_ns",
            source=str(message.get("source", "udp_pose")),
        )


def rotate_vector(
    orientation_xyzw: tuple[float, float, float, float],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    q = np.asarray(_normalize_quaternion(orientation_xyzw), dtype=np.float64)
    xyz = q[:3]
    value = np.asarray(vector, dtype=np.float64)
    rotated = value + 2.0 * q[3] * np.cross(xyz, value) + 2.0 * np.cross(xyz, np.cross(xyz, value))
    return tuple(rotated.tolist())  # type: ignore[return-value]


def transform_offset(
    pose: PoseSample, offset_in_lidar_m: tuple[float, float, float],
    *, child_frame_id: str,
) -> dict[str, Any]:
    rotated = rotate_vector(pose.orientation_xyzw, offset_in_lidar_m)
    position = tuple(a + b for a, b in zip(pose.position_m, rotated))
    return {
        "position_m": list(position),
        "orientation_xyzw": list(pose.orientation_xyzw),
        "frame_id": pose.frame_id,
        "child_frame_id": child_frame_id,
        "source_pose_timestamp_ns": pose.timestamp_ns,
        "offset_in_lidar_frame_m": list(offset_in_lidar_m),
    }


def _interpolate_quaternion(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    fraction: float,
) -> tuple[float, float, float, float]:
    left = np.asarray(first, dtype=np.float64)
    right = np.asarray(second, dtype=np.float64)
    if float(np.dot(left, right)) < 0:
        right = -right
    mixed = (1.0 - fraction) * left + fraction * right
    return _normalize_quaternion(mixed)


class PoseTimeline:
    def __init__(self, capacity: int = 20_000):
        self.capacity = capacity
        self._samples: list[PoseSample] = []
        self._lock = threading.RLock()

    def add(self, sample: PoseSample) -> None:
        with self._lock:
            if self._samples and sample.timestamp_ns >= self._samples[-1].timestamp_ns:
                self._samples.append(sample)
            else:
                index = bisect_left([item.timestamp_ns for item in self._samples], sample.timestamp_ns)
                self._samples.insert(index, sample)
            if len(self._samples) > self.capacity:
                del self._samples[:len(self._samples) - self.capacity]

    def latest(self) -> PoseSample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()

    def pose_at(self, timestamp_ns: int, max_age_ns: int) -> tuple[PoseSample | None, dict[str, Any]]:
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return None, {"available": False, "reason": "no lidar pose received"}
        times = [item.timestamp_ns for item in samples]
        right = bisect_left(times, timestamp_ns)
        if right == 0 or right == len(samples):
            selected = samples[0] if right == 0 else samples[-1]
            age = abs(timestamp_ns - selected.timestamp_ns)
            if age > max_age_ns:
                return None, {
                    "available": False, "reason": "nearest lidar pose is too old",
                    "nearest_age_ms": age / 1e6,
                }
            if selected.tracking_status not in TRACKING_STATES:
                return None, {"available": False, "reason": "lidar tracking is not valid"}
            return selected, {
                "available": True, "method": "nearest", "nearest_age_ms": age / 1e6,
                "interpolation_gap_ms": None,
            }
        before, after = samples[right - 1], samples[right]
        if before.frame_id != after.frame_id or before.child_frame_id != after.child_frame_id:
            return None, {"available": False, "reason": "pose frames changed across interpolation"}
        nearest_age = min(timestamp_ns - before.timestamp_ns, after.timestamp_ns - timestamp_ns)
        if nearest_age > max_age_ns:
            return None, {
                "available": False, "reason": "bracketing lidar poses are too far away",
                "nearest_age_ms": nearest_age / 1e6,
            }
        if before.tracking_status not in TRACKING_STATES or after.tracking_status not in TRACKING_STATES:
            return None, {"available": False, "reason": "lidar tracking is not valid"}
        interval = after.timestamp_ns - before.timestamp_ns
        fraction = 0.0 if interval == 0 else (timestamp_ns - before.timestamp_ns) / interval
        first_position = np.asarray(before.position_m, dtype=np.float64)
        second_position = np.asarray(after.position_m, dtype=np.float64)
        position = tuple(((1.0 - fraction) * first_position + fraction * second_position).tolist())
        pose = PoseSample(
            timestamp_ns=timestamp_ns,
            position_m=position,  # type: ignore[arg-type]
            orientation_xyzw=_interpolate_quaternion(
                before.orientation_xyzw, after.orientation_xyzw, fraction,
            ),
            frame_id=before.frame_id,
            child_frame_id=before.child_frame_id,
            tracking_status="TRACKING",
            timestamp_basis="monotonic_ns",
            source=f"interpolated:{before.source}",
        )
        return pose, {
            "available": True, "method": "interpolated",
            "nearest_age_ms": nearest_age / 1e6,
            "interpolation_gap_ms": interval / 1e6,
            "interpolation_fraction": fraction,
        }


class PoseProvider(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def latest(self) -> PoseSample | None: ...
    def pose_at(self, timestamp_ns: int) -> tuple[PoseSample | None, dict[str, Any]]: ...
    def metadata(self) -> dict[str, Any]: ...
    def reset_origin(self) -> None: ...
    def raw_latest(self) -> PoseSample | None: ...
    def set_origin_pose(self, origin: PoseSample, *, frame_id: str) -> None: ...


class NullPoseProvider:
    def start(self) -> None: pass
    def stop(self) -> None: pass
    def latest(self) -> PoseSample | None: return None

    def pose_at(self, _timestamp_ns: int) -> tuple[None, dict[str, Any]]:
        return None, {"available": False, "reason": "pose source disabled"}

    def metadata(self) -> dict[str, Any]:
        return {"source": "disabled", "received": 0, "rejected": 0}

    def reset_origin(self) -> None: pass
    def raw_latest(self) -> None: return None
    def set_origin_pose(self, origin: PoseSample, *, frame_id: str) -> None: pass


class ManualPoseProvider:
    """Hold user-entered coordinates until the next explicit manual update."""

    def __init__(
        self, position_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
        *, initial_timestamp_ns: int | None = None,
    ):
        self._lock = threading.RLock()
        self._updates: list[PoseSample] = []
        self.update(position_m, timestamp_ns=initial_timestamp_ns)

    @staticmethod
    def _sample(timestamp_ns: int, position_m: tuple[float, float, float]) -> PoseSample:
        return PoseSample(
            timestamp_ns=timestamp_ns,
            position_m=parse_vector3(position_m),
            orientation_xyzw=(0.0, 0.0, 0.0, 1.0),
            frame_id="manual_world",
            child_frame_id="manual_radar_position",
            tracking_status="MANUAL",
            timestamp_basis="monotonic_ns",
            source="manual_current_position",
        )

    def start(self) -> None: pass
    def stop(self) -> None: pass

    def update(
        self, position_m: tuple[float, float, float], *, timestamp_ns: int | None = None,
    ) -> None:
        timestamp = time.monotonic_ns() if timestamp_ns is None else int(timestamp_ns)
        if timestamp <= 0:
            raise ValueError("手动坐标时间戳必须为正数")
        sample = self._sample(timestamp, position_m)
        with self._lock:
            self._updates.append(sample)
            self._updates.sort(key=lambda item: item.timestamp_ns)

    def latest(self) -> PoseSample:
        with self._lock:
            position = self._updates[-1].position_m
        # Manual coordinates remain current until the user changes them, so
        # their display age should not expire like a streaming SLAM sample.
        return self._sample(time.monotonic_ns(), position)

    def pose_at(self, timestamp_ns: int) -> tuple[PoseSample, dict[str, Any]]:
        with self._lock:
            updates = list(self._updates)
        selected = updates[0]
        for update in updates:
            if update.timestamp_ns > timestamp_ns:
                break
            selected = update
        return self._sample(timestamp_ns, selected.position_m), {
            "available": True,
            "method": "manual_hold",
            "manual_update_timestamp_ns": selected.timestamp_ns,
            "manual_age_ms": max(0, timestamp_ns - selected.timestamp_ns) / 1e6,
        }

    def reset_origin(self) -> None:
        self.update((0.0, 0.0, 0.0))

    def raw_latest(self) -> PoseSample:
        return self.latest()

    def set_origin_pose(self, origin: PoseSample, *, frame_id: str) -> None:
        raise ValueError("手动位姿模式不支持远程共享原点，请使用 MID-360S UDP 位姿")

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            update_count = len(self._updates)
            last_update = self._updates[-1]
        return {
            "source": "manual",
            "mode": "manual_current_position",
            "received": update_count,
            "rejected": 0,
            "last_manual_update": asdict(last_update),
            "latest_pose": asdict(self.latest()),
        }


class UdpPoseProvider:
    """Receive AVTWIN_POSE_V1 poses already expressed on this PC's monotonic clock."""

    def __init__(
        self, host: str, port: int, max_age_s: float = 0.25,
        *, max_linear_speed_m_s: float = 5.0, jump_tolerance_m: float = 0.5,
    ):
        self.host = host
        self.port = port
        self.max_age_ns = round(max_age_s * 1e9)
        self.timeline = PoseTimeline()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._origin_lock = threading.Lock()
        self._users = 0
        self._origin: PoseSample | None = None
        self._relative_frame_id: str | None = None
        self._last_accepted_raw: PoseSample | None = None
        self.max_linear_speed_m_s = float(max_linear_speed_m_s)
        self.jump_tolerance_m = float(jump_tolerance_m)
        self.received = 0
        self.rejected = 0
        self.last_error: str | None = None
        self.last_rejection: str | None = None
        self.last_rejection_ns: int | None = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                self._users += 1
                return
            self._stop.clear()
            self._ready.clear()
            self.last_error = None
            self._users = 1
            self._thread = threading.Thread(target=self._run, name="avtwin-pose-udp", daemon=True)
            self._thread.start()
        self._ready.wait(timeout=1.0)
        if self.last_error:
            with self._lifecycle_lock:
                self._users = 0
            raise RuntimeError(f"雷达位姿 UDP 接口启动失败：{self.last_error}")

    def _run(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.host, self.port))
                listener.settimeout(0.2)
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        payload, _source = listener.recvfrom(65535)
                    except socket.timeout:
                        continue
                    received_ns = time.monotonic_ns()
                    try:
                        message = json.loads(payload.decode("utf-8"))
                        if not isinstance(message, dict):
                            raise ValueError("pose datagram 必须是 JSON object")
                        if message.get("protocol") not in {None, "AVTWIN_POSE_V1"}:
                            raise ValueError("不支持的 pose protocol")
                        sample = PoseSample.from_message(message, received_ns)
                        with self._origin_lock:
                            previous = self._last_accepted_raw
                            if previous is not None:
                                dt_s = max(0.0, (sample.timestamp_ns - previous.timestamp_ns) / 1e9)
                                # A short UDP/ROS pause must not turn into an unlimited
                                # jump allowance.  Indoor equipment cannot legitimately
                                # teleport several metres when FAST-LIO loses its map.
                                allowed_m = self.jump_tolerance_m + self.max_linear_speed_m_s * min(dt_s, 0.5)
                                distance_m = math.dist(sample.position_m, previous.position_m)
                                if distance_m > allowed_m:
                                    raise ValueError(
                                        "SLAM 位姿异常跳变："
                                        f"{distance_m:.3f}m/{dt_s:.3f}s，允许 {allowed_m:.3f}m；"
                                        "已保留上一有效坐标"
                                    )
                            self._last_accepted_raw = sample
                            if self._origin is None:
                                self._origin = sample
                                self._relative_frame_id = None
                            sample = relative_pose(
                                sample, self._origin, frame_id=self._relative_frame_id,
                            )
                            # Keep zero reset atomic with publishing the rebased
                            # sample so an old-frame sample cannot reappear.
                            self.timeline.add(sample)
                        self.received += 1
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                        self.rejected += 1
                        self.last_rejection = str(exc)
                        self.last_rejection_ns = time.monotonic_ns()
        except OSError as exc:
            self.last_error = str(exc)
            self._ready.set()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._users <= 0:
                return
            self._users -= 1
            if self._users > 0:
                return
            self._stop.set()
            thread = self._thread
        if thread:
            thread.join(timeout=1.0)
        with self._lifecycle_lock:
            if self._thread is thread:
                self._thread = None

    def latest(self) -> PoseSample | None:
        return self.timeline.latest()

    def raw_latest(self) -> PoseSample | None:
        with self._origin_lock:
            return self._last_accepted_raw

    def set_origin_pose(self, origin: PoseSample, *, frame_id: str) -> None:
        """Rebase future raw SLAM poses to an explicit shared origin pose."""
        if not frame_id.strip():
            raise ValueError("共享 frame_id 不能为空")
        with self._origin_lock:
            raw = self._last_accepted_raw
            if raw is None:
                raise ValueError("尚未收到 MID-360S 原始位姿")
            if origin.frame_id != raw.frame_id:
                raise ValueError(
                    f"共享原点 frame {origin.frame_id!r} 与雷达 frame {raw.frame_id!r} 不一致"
                )
            self._origin = origin
            self._relative_frame_id = frame_id.strip()
            self.timeline.clear()
            self.timeline.add(relative_pose(raw, origin, frame_id=self._relative_frame_id))

    def reset_origin(self) -> None:
        """Make the next received pose the new zero without restarting ROS."""
        with self._origin_lock:
            self._origin = None
            self._relative_frame_id = None
            self._last_accepted_raw = None
            self.timeline.clear()
            self.last_rejection = None
            self.last_rejection_ns = None

    def pose_at(self, timestamp_ns: int) -> tuple[PoseSample | None, dict[str, Any]]:
        return self.timeline.pose_at(timestamp_ns, self.max_age_ns)

    def metadata(self) -> dict[str, Any]:
        latest = self.latest()
        return {
            "source": "udp", "listen_host": self.host, "listen_port": self.port,
            "received": self.received, "rejected": self.rejected,
            "last_error": self.last_error, "last_rejection": self.last_rejection,
            "last_rejection_ns": self.last_rejection_ns,
            "latest_pose": None if latest is None else asdict(latest),
            "required_protocol": "AVTWIN_POSE_V1",
        }


def _multiply_quaternions(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    lx, ly, lz, lw = left
    rx, ry, rz, rw = right
    return _normalize_quaternion((
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    ))


def relative_pose(
    sample: PoseSample, origin: PoseSample, *, frame_id: str | None = None,
) -> PoseSample:
    """Express a pose in a coordinate frame fixed to an origin pose."""
    inverse_origin = (
        -origin.orientation_xyzw[0], -origin.orientation_xyzw[1],
        -origin.orientation_xyzw[2], origin.orientation_xyzw[3],
    )
    delta = tuple(
        current - initial
        for current, initial in zip(sample.position_m, origin.position_m)
    )
    return PoseSample(
        timestamp_ns=sample.timestamp_ns,
        position_m=rotate_vector(inverse_origin, delta),
        orientation_xyzw=_multiply_quaternions(inverse_origin, sample.orientation_xyzw),
        frame_id=frame_id or f"{sample.frame_id}/user_zero",
        child_frame_id=sample.child_frame_id,
        tracking_status=sample.tracking_status,
        timestamp_basis=sample.timestamp_basis,
        source=f"relative:{sample.source}",
    )
