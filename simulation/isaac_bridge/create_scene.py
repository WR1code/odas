"""Create/query the reference USD scene using the locally installed Isaac Sim API.

Run this file with Isaac Sim's python.sh, never with the AcoustiX environment.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--usd", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    from isaacsim import SimulationApp
    app = SimulationApp({"headless": args.headless})
    try:
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

        source = json.loads(args.scene.read_text(encoding="utf-8"))
        args.usd.parent.mkdir(parents=True, exist_ok=True)
        stage = Usd.Stage.CreateNew(str(args.usd.resolve()))
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        world = UsdGeom.Xform.Define(stage, "/World")

        for item in source["objects"]:
            name = item["name"].replace(" ", "_")
            cube = UsdGeom.Cube.Define(stage, f"/World/AcousticGeometry/{name}")
            cube.CreateSizeAttr(1.0)
            xf = UsdGeom.Xformable(cube)
            xf.AddTranslateOp().Set(Gf.Vec3d(*item["center_m"]))
            xf.AddScaleOp().Set(Gf.Vec3d(*item["size_m"]))
            prim = cube.GetPrim()
            prim.CreateAttribute("acoustics:material", Sdf.ValueTypeNames.String, custom=True).Set(item["material"])
            prim.CreateAttribute("semantics:class", Sdf.ValueTypeNames.String, custom=True).Set(item["name"])
            UsdPhysics.CollisionAPI.Apply(prim)

        array_data = source["array_pose"]
        array = UsdGeom.Xform.Define(stage, "/World/Robot/Uma8Array")
        array_xf = UsdGeom.Xformable(array)
        array_xf.AddTranslateOp().Set(Gf.Vec3d(*array_data["translation_m"]))
        w, x, y, z = array_data["quaternion_wxyz"]
        array_xf.AddOrientOp().Set(Gf.Quatf(w, Gf.Vec3f(x, y, z)))

        project_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(project_root))
        from simulation.common.geometry import UMA8_ACTIVE_MICS_M
        for index, point in enumerate(UMA8_ACTIVE_MICS_M, start=1):
            mic = UsdGeom.Sphere.Define(stage, f"/World/Robot/Uma8Array/Mic{index}")
            mic.CreateRadiusAttr(0.006)
            UsdGeom.Xformable(mic).AddTranslateOp().Set(Gf.Vec3d(*point.tolist()))
            mic.GetPrim().CreateAttribute("semantics:class", Sdf.ValueTypeNames.String, custom=True).Set("microphone")

        speaker = UsdGeom.Sphere.Define(stage, "/World/Sources/Speaker1")
        speaker.CreateRadiusAttr(0.08)
        UsdGeom.Xformable(speaker).AddTranslateOp().Set(Gf.Vec3d(*source["source"]["position_m"]))
        speaker.GetPrim().CreateAttribute("semantics:class", Sdf.ValueTypeNames.String, custom=True).Set("sound_source")

        stage.SetDefaultPrim(world.GetPrim())
        stage.GetRootLayer().Save()

        def world_transform(path: str):
            return UsdGeom.Xformable(stage.GetPrimAtPath(path)).ComputeLocalToWorldTransform(Usd.TimeCode.Default())

        def translation(path: str) -> list[float]:
            transform = world_transform(path)
            value = transform.ExtractTranslation()
            return [float(value[0]), float(value[1]), float(value[2])]

        def quaternion_wxyz(path: str) -> list[float]:
            value = world_transform(path).ExtractRotationQuat()
            imag = value.GetImaginary()
            return [float(value.GetReal()), float(imag[0]), float(imag[1]), float(imag[2])]

        state = dict(source)
        state["source"]["position_m"] = translation("/World/Sources/Speaker1")
        state["array_pose"]["translation_m"] = translation("/World/Robot/Uma8Array")
        state["array_pose"]["quaternion_wxyz"] = quaternion_wxyz("/World/Robot/Uma8Array")
        state["microphones_world_m"] = [translation(f"/World/Robot/Uma8Array/Mic{i}") for i in range(1, 8)]
        state["usd_path"] = str(args.usd.resolve())
        state["isaac_version"] = "6.0.1"
        args.state.parent.mkdir(parents=True, exist_ok=True)
        args.state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        args.state.with_suffix(".error.txt").unlink(missing_ok=True)
        print(json.dumps({"usd": str(args.usd), "state": str(args.state), "microphones": 7}))
    except BaseException:
        import traceback
        failure = args.state.with_suffix(".error.txt")
        failure.parent.mkdir(parents=True, exist_ok=True)
        failure.write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        app.close()


# Isaac Sim 6 standalone executes scripts with ``__name__ == "__mp_main__"``
# on this installation, so use the same import guard employed by its examples.
if __name__ in {"__main__", "__mp_main__"}:
    main()
