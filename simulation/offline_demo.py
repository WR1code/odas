"""Reproducible Isaac -> AcoustiX -> PCM -> ODAS offline integration demo."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import wave

import numpy as np

from simulation.acoustix_service.client import AcoustixUnavailable, compute_rirs
from simulation.acoustix_service.scene_converter import convert_scene
from simulation.common.audio import convolve_mono_rirs, normalize_peak, read_mono_wav, with_zero_hardware_channel, write_pcm_wav, write_raw_s32le
from simulation.common.geometry import Pose, UMA8_ACTIVE_MICS_M, direction_in_array, spherical_from_vector, transform_points
from simulation.common.odas import evaluate_tracks


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT / "simulation/output/offline"


def run_isaac(scene: Path, output: Path, isaac_root: Path) -> Path:
    python = isaac_root / "python.sh"
    if not python.is_file():
        raise RuntimeError(f"Isaac Sim python.sh not found: {python}")
    state = output / "isaac_scene_state.json"
    command = [str(python), str(PROJECT / "simulation/isaac_bridge/create_scene.py"),
               "--scene", str(scene), "--usd", str(output / "test_room.usda"), "--state", str(state), "--headless"]
    subprocess.run(command, cwd=PROJECT, check=True)
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speech", type=Path, required=True, help="mono clean-speech WAV; it is resampled to 48 kHz")
    parser.add_argument("--scene", type=Path, default=PROJECT / "simulation/scenes/test_room.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--isaac-root", type=Path, default=Path("/home/w/Desktop/isaac-sim-standalone-6.0.1-linux-x86_64"))
    parser.add_argument("--skip-isaac", action="store_true", help="use scene JSON directly for developer-only testing")
    parser.add_argument("--skip-odas", action="store_true")
    parser.add_argument("--seed", type=int, default=20260809)
    parser.add_argument("--conda-env", default="odas-acoustix")
    args = parser.parse_args()
    if not args.speech.is_file():
        parser.error(f"clean-speech WAV does not exist: {args.speech}")
    try:
        clean = read_mono_wav(args.speech, 48000)
    except (OSError, EOFError, wave.Error) as error:
        parser.error(f"cannot read clean-speech WAV {args.speech}: {error}")
    np.random.seed(args.seed)
    args.output.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    if args.skip_isaac:
        scene_state_path = args.output / "isaac_scene_state.json"
        shutil.copyfile(args.scene, scene_state_path)
    else:
        scene_state_path = run_isaac(args.scene, args.output, args.isaac_root)
    scene = json.loads(scene_state_path.read_text(encoding="utf-8"))
    pose = Pose.from_values(scene["array_pose"]["translation_m"], scene["array_pose"]["quaternion_wxyz"])
    microphones = np.asarray(scene.get("microphones_world_m", transform_points(pose, UMA8_ACTIVE_MICS_M)), dtype=float)
    source = np.asarray(scene["source"]["position_m"], dtype=float)
    truth = spherical_from_vector(direction_in_array(source, pose))
    acoustic_scene = convert_scene(scene_state_path, PROJECT / "simulation/configs/materials.json", args.output / "acoustix_scene")

    rir_path = args.output / "rirs.npz"
    try:
        rirs, rir_status = compute_rirs(
            scene_xml=acoustic_scene, config=PROJECT / "simulation/configs/acoustix_48k.yml",
            source_position_m=source, receiver_positions_m=microphones, output_npz=rir_path,
            seed=args.seed, project_root=PROJECT, conda_env=args.conda_env,
            materials_path=PROJECT / "simulation/configs/materials.json",
        )
    except AcoustixUnavailable as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 3

    active = convolve_mono_rirs(clean, rirs)
    active, gain = normalize_peak(active, 0.8)
    eight = with_zero_hardware_channel(active)
    remainder = len(eight) % 512
    if remainder:
        eight = np.pad(eight, ((0, 512 - remainder), (0, 0)))
    raw_stats = write_raw_s32le(args.output / "multichannel_s32le.raw", eight)
    write_pcm_wav(args.output / "multichannel.wav", eight)

    tracks = args.output / "tracks.json"
    if tracks.exists():
        tracks.unlink()
    odas_metrics = None
    if not args.skip_odas:
        config_template = (PROJECT / "config/odaslive/uma8_sim_file.cfg").read_text(encoding="utf-8")
        runtime_config = args.output / "odas_file.cfg"
        runtime_config.write_text(
            config_template
            .replace("/home/w/project/odas/simulation/output/offline/multichannel_s32le.raw", str(args.output.resolve() / "multichannel_s32le.raw"))
            .replace("/home/w/project/odas/simulation/output/offline/tracks.json", str(tracks.resolve())),
            encoding="utf-8",
        )
        subprocess.run([str(PROJECT / "build/bin/odaslive"), "-s", "-c", str(runtime_config)], cwd=PROJECT, check=True)
        odas_metrics = evaluate_tracks(tracks, truth)
    report = {
        "backend": "official_acoustix", "seed": args.seed, "truth": truth,
        "channel_8_policy": "zero; ODAS mapping (1..7) ignores raw hardware channel 8",
        "pcm": {**raw_stats, "sample_rate_hz": 48000, "sample_format": "signed 32-bit little-endian interleaved"},
        "normalization_gain": gain, "rir": rir_status, "odas": odas_metrics,
        "wall_seconds": time.perf_counter() - started,
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
