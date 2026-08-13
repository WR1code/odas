#!/usr/bin/env python3
"""Bridge a ROS 2 SLAM pose topic to AV-Twin's monotonic UDP pose protocol."""
from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
from typing import Any


SUPPORTED_TYPES = {
    "nav_msgs/msg/Odometry": "odometry",
    "geometry_msgs/msg/PoseStamped": "pose_stamped",
    "geometry_msgs/msg/PoseWithCovarianceStamped": "pose_with_covariance_stamped",
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="把 ROS 2 SLAM 位姿转发到 AV-Twin AVTWIN_POSE_V1 UDP 接口",
    )
    result.add_argument("--topic", default="/Odometry", help="ROS 2 位姿 topic")
    result.add_argument(
        "--message-type", choices=("auto", *SUPPORTED_TYPES.values()), default="auto",
        help="topic 消息类型；auto 会从 ROS graph 自动识别",
    )
    result.add_argument("--host", default="127.0.0.1", help="AV-Twin UDP 接收地址")
    result.add_argument("--port", type=int, default=5006, help="AV-Twin UDP 接收端口")
    result.add_argument("--source", default="mid360s_slam_ros2", help="写入位姿元数据的来源名")
    result.add_argument("--child-frame-id", default="livox_frame", help="消息未提供 child frame 时的值")
    result.add_argument("--discovery-timeout", type=float, default=0.0, help="auto 等待 topic 的秒数；0=一直等待")
    result.add_argument(
        "--relative-origin", action=argparse.BooleanOptionalAction, default=False,
        help="在桥接层以第一帧为原点；声学 GUI 已自行管理可重置零点，通常无需启用",
    )
    return result


def _pose_parts(message: Any, message_type: str) -> tuple[Any, str, str]:
    if message_type == "odometry":
        return message.pose.pose, str(message.header.frame_id), str(message.child_frame_id)
    if message_type == "pose_stamped":
        return message.pose, str(message.header.frame_id), ""
    if message_type == "pose_with_covariance_stamped":
        return message.pose.pose, str(message.header.frame_id), ""
    raise ValueError(f"不支持的 ROS 位姿类型：{message_type}")


def pose_message(
    message: Any, message_type: str, *, received_monotonic_ns: int,
    source: str, fallback_child_frame_id: str,
) -> dict[str, Any]:
    pose, frame_id, child_frame_id = _pose_parts(message, message_type)
    return {
        "protocol": "AVTWIN_POSE_V1",
        "type": "lidar_pose",
        # ROS header stamps may use wall, simulation, PTP, or sensor time. Capturing
        # CLOCK_MONOTONIC in this callback is the only safe basis for AV-Twin audio.
        "timestamp_basis": "monotonic_ns",
        "timestamp_ns": int(received_monotonic_ns),
        "position_m": [
            float(pose.position.x), float(pose.position.y), float(pose.position.z),
        ],
        "orientation_xyzw": [
            float(pose.orientation.x), float(pose.orientation.y),
            float(pose.orientation.z), float(pose.orientation.w),
        ],
        "frame_id": frame_id or "map",
        "child_frame_id": child_frame_id or fallback_child_frame_id,
        "tracking_status": "TRACKING",
        "source": source,
        "timestamp_note": "CLOCK_MONOTONIC captured at ROS callback receipt",
    }


def _normalize_quaternion(value: list[float]) -> tuple[float, float, float, float]:
    norm = math.sqrt(sum(component * component for component in value))
    if norm < 1e-12:
        raise ValueError("ROS 位姿包含零四元数")
    return tuple(component / norm for component in value)  # type: ignore[return-value]


def _quaternion_multiply(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    lx, ly, lz, lw = left
    rx, ry, rz, rw = right
    return (
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    )


def _rotate_vector(
    quaternion: tuple[float, float, float, float], vector: list[float],
) -> list[float]:
    vector_quaternion = (vector[0], vector[1], vector[2], 0.0)
    conjugate = (-quaternion[0], -quaternion[1], -quaternion[2], quaternion[3])
    rotated = _quaternion_multiply(
        _quaternion_multiply(quaternion, vector_quaternion), conjugate,
    )
    return [rotated[0], rotated[1], rotated[2]]


def rebase_pose_message(
    message: dict[str, Any], origin: dict[str, Any],
) -> dict[str, Any]:
    """Express a pose relative to the first bridge sample."""
    origin_position = [float(item) for item in origin["position_m"]]
    position = [float(item) for item in message["position_m"]]
    origin_orientation = _normalize_quaternion(
        [float(item) for item in origin["orientation_xyzw"]],
    )
    orientation = _normalize_quaternion(
        [float(item) for item in message["orientation_xyzw"]],
    )
    inverse_origin = (
        -origin_orientation[0], -origin_orientation[1],
        -origin_orientation[2], origin_orientation[3],
    )
    relative_orientation = _normalize_quaternion(list(_quaternion_multiply(inverse_origin, orientation)))
    result = dict(message)
    result["position_m"] = _rotate_vector(
        inverse_origin, [value - base for value, base in zip(position, origin_position)],
    )
    result["orientation_xyzw"] = list(relative_orientation)
    result["frame_id"] = f"{message.get('frame_id', 'map')}/relative_start"
    result["coordinate_mode"] = "relative_to_bridge_start"
    result["origin_timestamp_ns"] = int(origin["timestamp_ns"])
    return result


def _ros_class(message_type: str) -> type[Any]:
    if message_type == "odometry":
        from nav_msgs.msg import Odometry
        return Odometry
    if message_type == "pose_stamped":
        from geometry_msgs.msg import PoseStamped
        return PoseStamped
    if message_type == "pose_with_covariance_stamped":
        from geometry_msgs.msg import PoseWithCovarianceStamped
        return PoseWithCovarianceStamped
    raise ValueError(f"不支持的 ROS 位姿类型：{message_type}")


def _discover(node: Any, topic: str, timeout_s: float) -> str:
    started = time.monotonic()
    last_notice = 0.0
    while True:
        for name, types in node.get_topic_names_and_types():
            if name != topic:
                continue
            supported = [item for item in types if item in SUPPORTED_TYPES]
            if not supported:
                raise RuntimeError(f"{topic} 的类型 {types} 不受支持；支持 {list(SUPPORTED_TYPES)}")
            selected = SUPPORTED_TYPES[supported[0]]
            print(f"ROS 位姿 topic 已发现：{topic} [{supported[0]}]", flush=True)
            return selected
        elapsed = time.monotonic() - started
        if timeout_s > 0 and elapsed >= timeout_s:
            raise TimeoutError(f"{timeout_s:g}s 内未发现 ROS 位姿 topic {topic}")
        if elapsed - last_notice >= 5.0 or last_notice == 0.0:
            print(f"等待 ROS SLAM 位姿 topic：{topic} ...", flush=True)
            last_notice = elapsed
        import rclpy
        rclpy.spin_once(node, timeout_sec=0.2)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser().error("--port 必须在 1..65535 内")

    try:
        import rclpy
        from rclpy.executors import ExternalShutdownException
        from rclpy.qos import qos_profile_sensor_data
    except ImportError as exc:
        print("错误：无法导入 ROS 2 Jazzy rclpy；请先 source /opt/ros/jazzy/setup.bash", file=sys.stderr)
        return 2

    rclpy.init(args=[])
    node = rclpy.create_node("avtwin_mid360s_pose_bridge")
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    last_report = time.monotonic()
    origin: dict[str, Any] | None = None
    try:
        selected_type = args.message_type
        if selected_type == "auto":
            selected_type = _discover(node, args.topic, args.discovery_timeout)
        ros_type = _ros_class(selected_type)

        def callback(message: Any) -> None:
            nonlocal sent, last_report, origin
            payload = pose_message(
                message, selected_type, received_monotonic_ns=time.monotonic_ns(),
                source=args.source, fallback_child_frame_id=args.child_frame_id,
            )
            if args.relative_origin:
                if origin is None:
                    origin = payload
                    print("MID-360S 相对坐标原点已建立：第一帧 = (0,0,0)", flush=True)
                payload = rebase_pose_message(payload, origin)
            udp.sendto(json.dumps(payload, separators=(",", ":")).encode("utf-8"), (args.host, args.port))
            sent += 1
            now = time.monotonic()
            if now - last_report >= 5.0:
                print(
                    f"位姿桥运行中：{args.topic} -> udp://{args.host}:{args.port}，已发送 {sent}",
                    flush=True,
                )
                last_report = now

        node.create_subscription(ros_type, args.topic, callback, qos_profile_sensor_data)
        print(
            f"AV-Twin ROS 位姿桥已启动：{args.topic} ({selected_type}) -> "
            f"udp://{args.host}:{args.port}", flush=True,
        )
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except TimeoutError as exc:
        print(f"位姿桥失败：{exc}", file=sys.stderr)
        return 2
    finally:
        udp.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
