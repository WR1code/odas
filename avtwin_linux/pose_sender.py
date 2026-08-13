from __future__ import annotations

import argparse
import json
import socket
import time


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="发送测试/桥接雷达位姿到 AV-Twin UDP PoseProvider")
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=5006)
    result.add_argument("--position", nargs=3, type=float, metavar=("X", "Y", "Z"), required=True)
    result.add_argument("--quaternion", nargs=4, type=float, metavar=("QX", "QY", "QZ", "QW"), default=(0, 0, 0, 1))
    result.add_argument("--frame-id", default="map")
    result.add_argument("--child-frame-id", default="livox_frame")
    result.add_argument("--rate", type=float, default=10.0, help="发送频率 Hz；0=只发送一次")
    result.add_argument("--duration", type=float, default=0.0, help="发送秒数；0=直到 Ctrl+C")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.rate < 0 or args.duration < 0:
        raise ValueError("rate 和 duration 不能为负数")
    interval = None if args.rate == 0 else 1.0 / args.rate
    started = time.monotonic()
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        try:
            while True:
                # This helper runs on the same Linux host, so CLOCK_MONOTONIC
                # is directly comparable with the audio sample clock mapping.
                message = {
                    "protocol": "AVTWIN_POSE_V1", "type": "lidar_pose",
                    "timestamp_basis": "monotonic_ns", "timestamp_ns": time.monotonic_ns(),
                    "position_m": args.position, "orientation_xyzw": args.quaternion,
                    "frame_id": args.frame_id, "child_frame_id": args.child_frame_id,
                    "tracking_status": "TRACKING", "source": "pose_sender",
                }
                sender.sendto(json.dumps(message, separators=(",", ":")).encode(), (args.host, args.port))
                sent += 1
                if interval is None or (args.duration and time.monotonic() - started >= args.duration):
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    print(f"sent {sent} pose message(s) to {args.host}:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
