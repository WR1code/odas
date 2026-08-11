"""Isaac 6.0.1 process publishing simulation clock, TF, array, source, and robot poses."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--usd", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--physics-hz", type=float, default=60.0)
    parser.add_argument("--source-amplitude-m", type=float, default=0.5)
    parser.add_argument("--source-frequency-hz", type=float, default=0.1)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    from isaacsim import SimulationApp
    app = SimulationApp({"headless": args.headless})
    node = None
    log_handle = None
    try:
        import rclpy
        from rclpy.parameter import Parameter
        from geometry_msgs.msg import PoseStamped, TransformStamped
        from rosgraph_msgs.msg import Clock
        from tf2_msgs.msg import TFMessage
        from pxr import Gf, Usd, UsdGeom

        stage = Usd.Stage.Open(str(args.usd.resolve()))
        source_prim = stage.GetPrimAtPath("/World/Sources/Speaker1")
        source_xf = UsdGeom.Xformable(source_prim)
        translate_op = next(op for op in source_xf.GetOrderedXformOps() if op.GetOpType() == UsdGeom.XformOp.TypeTranslate)
        initial = translate_op.Get()

        rclpy.init()
        node = rclpy.create_node("isaac_acoustic_scene_bridge")
        node.set_parameters([Parameter("use_sim_time", Parameter.Type.BOOL, True)])
        clock_pub = node.create_publisher(Clock, "/clock", 10)
        tf_pub = node.create_publisher(TFMessage, "/tf", 10)
        array_pub = node.create_publisher(PoseStamped, "/acoustics/array_pose", 10)
        source_pub = node.create_publisher(PoseStamped, "/acoustics/source_pose", 10)
        robot_pub = node.create_publisher(PoseStamped, "/robot/pose", 10)
        if args.log:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            log_handle = args.log.open("w", encoding="utf-8")

        def stamp(seconds: float):
            msg = Clock()
            msg.clock.sec = int(seconds)
            msg.clock.nanosec = int(round((seconds - int(seconds)) * 1e9))
            return msg

        def world_matrix(path: str):
            return UsdGeom.Xformable(stage.GetPrimAtPath(path)).ComputeLocalToWorldTransform(Usd.TimeCode.Default())

        def world_translation(path: str):
            matrix = world_matrix(path)
            value = matrix.ExtractTranslation()
            return float(value[0]), float(value[1]), float(value[2])

        def world_quaternion_xyzw(path: str):
            value = world_matrix(path).ExtractRotationQuat()
            imag = value.GetImaginary()
            return float(imag[0]), float(imag[1]), float(imag[2]), float(value.GetReal())

        steps = int(round(args.duration * args.physics_hz))
        for step in range(steps):
            sim_time = step / args.physics_hz
            translate_op.Set(Gf.Vec3d(initial[0], initial[1] + args.source_amplitude_m * math.sin(2*math.pi*args.source_frequency_hz*sim_time), initial[2]))
            app.update()
            now = stamp(sim_time)
            clock_pub.publish(now)
            array_xyz = world_translation("/World/Robot/Uma8Array")
            array_q = world_quaternion_xyzw("/World/Robot/Uma8Array")
            source_xyz = world_translation("/World/Sources/Speaker1")

            def pose_message(topic_frame: str, xyz, quaternion=(0.0, 0.0, 0.0, 1.0)):
                msg = PoseStamped()
                msg.header.stamp = now.clock
                msg.header.frame_id = topic_frame
                msg.pose.position.x, msg.pose.position.y, msg.pose.position.z = xyz
                msg.pose.orientation.x, msg.pose.orientation.y, msg.pose.orientation.z, msg.pose.orientation.w = quaternion
                return msg
            array_msg = pose_message("world", array_xyz, array_q)
            source_msg = pose_message("world", source_xyz)
            array_pub.publish(array_msg)
            source_pub.publish(source_msg)
            robot_pub.publish(array_msg)

            transforms = []
            parent_items = [("world", "uma8_array", array_xyz, array_q),
                            ("world", "sound_source_1", source_xyz, (0.0, 0.0, 0.0, 1.0))]
            array_inverse = world_matrix("/World/Robot/Uma8Array").GetInverse()
            for i in range(1, 8):
                world_xyz = world_translation(f"/World/Robot/Uma8Array/Mic{i}")
                local = array_inverse.Transform(Gf.Vec3d(*world_xyz))
                parent_items.append(("uma8_array", f"mic_{i}", tuple(local), (0.0, 0.0, 0.0, 1.0)))
            for parent, child, xyz, quaternion in parent_items:
                transform = TransformStamped()
                transform.header.stamp = now.clock
                transform.header.frame_id = parent
                transform.child_frame_id = child
                transform.transform.translation.x, transform.transform.translation.y, transform.transform.translation.z = xyz
                (transform.transform.rotation.x, transform.transform.rotation.y,
                 transform.transform.rotation.z, transform.transform.rotation.w) = quaternion
                transforms.append(transform)
            tf_pub.publish(TFMessage(transforms=transforms))
            rclpy.spin_once(node, timeout_sec=0.0)
            if log_handle:
                log_handle.write(json.dumps({"sim_time_s": sim_time, "source_position_m": source_xyz,
                                             "array_position_m": array_xyz}) + "\n")
            time.sleep(max(0.0, 1.0 / args.physics_hz))
        if args.log:
            args.log.with_suffix(".error.txt").unlink(missing_ok=True)
    except BaseException:
        import traceback
        failure = (args.log or Path("/tmp/isaac_ros_bridge")).with_suffix(".error.txt")
        failure.parent.mkdir(parents=True, exist_ok=True)
        failure.write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if log_handle:
            log_handle.close()
        if node is not None:
            node.destroy_node()
            import rclpy
            rclpy.shutdown()
        app.close()


if __name__ in {"__main__", "__mp_main__"}:
    main()
