"""Batch AcoustiX dataset generator using WAV + JSON manifest + NPZ RIR.

Every emitted RIR comes from the official out-of-process AcoustiX worker.  The
generator aborts if that backend is unavailable; there is no fallback simulator.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess

import numpy as np

from simulation.acoustix_service.client import compute_rirs
from simulation.acoustix_service.scene_converter import convert_scene
from simulation.common.audio import convolve_mono_rirs, normalize_peak, read_mono_wav, with_zero_hardware_channel, write_pcm_wav, write_raw_s32le
from simulation.common.geometry import Pose, UMA8_ACTIVE_MICS_M, direction_in_array, spherical_from_vector, transform_points
from simulation.common.odas import evaluate_tracks


PROJECT = Path(__file__).resolve().parents[2]


def impair(audio: np.ndarray, rng: np.random.Generator, snr_db: float) -> tuple[np.ndarray, dict]:
    gains_db = rng.normal(0.0, 0.75, audio.shape[1])
    delays = rng.normal(0.0, 0.35, audio.shape[1])
    drift_ppm = rng.normal(0.0, 15.0, audio.shape[1])
    output = np.zeros_like(audio)
    samples = np.arange(len(audio), dtype=float)
    for channel in range(audio.shape[1]):
        warped = samples * (1.0 + drift_ppm[channel] * 1e-6) - delays[channel]
        output[:, channel] = np.interp(warped, samples, audio[:, channel], left=0.0, right=0.0)
        output[:, channel] *= 10.0 ** (gains_db[channel] / 20.0)
    power = float(np.mean(output**2))
    noise_std = math.sqrt(power / (10.0 ** (snr_db / 10.0))) if power else 0.0
    output += rng.normal(0.0, noise_std, output.shape)
    clip_level = float(rng.uniform(0.82, 1.0))
    output = np.clip(output, -clip_level, clip_level)
    return output, {"microphone_gain_db": gains_db.tolist(), "fractional_delay_samples": delays.tolist(),
                    "clock_drift_ppm": drift_ppm.tolist(), "snr_db": snr_db,
                    "sensor_noise_std": noise_std, "clip_level": clip_level}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speech", type=Path, nargs="+", required=True)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260809)
    parser.add_argument("--output", type=Path, default=PROJECT / "simulation/output/dataset")
    parser.add_argument("--conda-env", default="odas-acoustix")
    parser.add_argument("--max-sources", type=int, default=2)
    parser.add_argument("--skip-odas", action="store_true")
    args = parser.parse_args()
    rng = np.random.default_rng(args.seed)
    base = json.loads((PROJECT / "simulation/scenes/test_room.json").read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as manifest:
        for index in range(args.samples):
            sample_seed = int(rng.integers(0, 2**31 - 1))
            sample_dir = args.output / f"sample_{index:06d}"
            sample_dir.mkdir(parents=True, exist_ok=True)
            scene = json.loads(json.dumps(base))
            for obj in scene["objects"]:
                if obj["name"] in {"table_top", "cabinet"}:
                    obj["center_m"][0] += float(rng.uniform(-0.25, 0.25))
                    obj["center_m"][1] += float(rng.uniform(-0.25, 0.25))
            source_count = int(rng.integers(1, args.max_sources + 1))
            sources = [[float(rng.uniform(0.8, 2.4)), float(rng.uniform(-1.5, 1.5)),
                        float(rng.uniform(1.0, 1.8))] for _ in range(source_count)]
            scene["source"]["position_m"] = sources[0]
            yaw = float(rng.uniform(-math.pi, math.pi))
            scene["array_pose"]["translation_m"] = [float(rng.uniform(-1.0, 1.0)), float(rng.uniform(-1.0, 1.0)), 1.2]
            scene["array_pose"]["quaternion_wxyz"] = [math.cos(yaw/2), 0.0, 0.0, math.sin(yaw/2)]
            scene_path = sample_dir / "scene.json"
            scene_path.write_text(json.dumps(scene, indent=2) + "\n")
            pose = Pose.from_values(scene["array_pose"]["translation_m"], scene["array_pose"]["quaternion_wxyz"])
            local_mics = UMA8_ACTIVE_MICS_M + rng.normal(0.0, 0.0005, UMA8_ACTIVE_MICS_M.shape)
            mics = transform_points(pose, local_mics)
            material_data = json.loads((PROJECT / "simulation/configs/materials.json").read_text())
            for material in material_data["materials"].values():
                material["absorption"] = np.clip(np.asarray(material["absorption"]) * rng.uniform(0.9, 1.1, 6), 0.0, 0.99).tolist()
            materials_path = sample_dir / "materials.json"
            materials_path.write_text(json.dumps(material_data, indent=2) + "\n")
            xml = convert_scene(scene_path, materials_path, sample_dir / "acoustix_scene")
            rir_path = sample_dir / "rirs.npz"
            all_rirs, backend_stats, rendered = [], [], []
            speech_paths = []
            for source_index, source_position in enumerate(sources):
                source_rir_path = sample_dir / f"rirs_source_{source_index}.npz"
                rirs, stats = compute_rirs(scene_xml=xml, config=PROJECT / "simulation/configs/acoustix_48k.yml",
                    source_position_m=np.asarray(source_position), receiver_positions_m=mics,
                    output_npz=source_rir_path, seed=sample_seed + source_index, project_root=PROJECT,
                    conda_env=args.conda_env, materials_path=materials_path)
                speech_path = args.speech[(index + source_index) % len(args.speech)]
                speech_paths.append(str(speech_path.resolve()))
                rendered.append(convolve_mono_rirs(read_mono_wav(speech_path), rirs))
                all_rirs.append(rirs)
                backend_stats.append(stats)
            np.savez_compressed(rir_path, rirs=np.stack(all_rirs), source_positions_m=np.asarray(sources),
                                microphone_positions_m=mics)
            max_length = max(len(item) for item in rendered)
            active = np.zeros((max_length, 7))
            for item in rendered:
                active[:len(item)] += item
            active, gain = normalize_peak(active, 0.8)
            snr_db = float(rng.uniform(5.0, 30.0))
            active, impairments = impair(active, rng, snr_db)
            audio = with_zero_hardware_channel(active)
            wav_path = sample_dir / "audio.wav"
            write_pcm_wav(wav_path, audio)
            raw_path = sample_dir / "audio.raw"
            padded = np.pad(audio, ((0, (-len(audio)) % 512), (0, 0)))
            write_raw_s32le(raw_path, padded)
            truth = [spherical_from_vector(direction_in_array(position, pose)) for position in sources]
            odas_estimate = None
            if not args.skip_odas:
                tracks_path = sample_dir / "odas_tracks.json"
                config_text = (PROJECT / "config/odaslive/uma8_sim_file.cfg").read_text()
                config_text = config_text.replace("/home/w/project/odas/simulation/output/offline/multichannel_s32le.raw", str(raw_path.resolve()))
                config_text = config_text.replace("/home/w/project/odas/simulation/output/offline/tracks.json", str(tracks_path.resolve()))
                sample_config = sample_dir / "odas.cfg"
                sample_config.write_text(config_text)
                subprocess.run([str(PROJECT / "build/bin/odaslive"), "-s", "-c", str(sample_config)], check=True)
                odas_estimate = evaluate_tracks(tracks_path, truth[0])
            entry = {"sample_id": index, "seed": sample_seed, "audio_wav": str(wav_path.relative_to(args.output)),
                     "rir_npz": str(rir_path.relative_to(args.output)), "scene": str(scene_path.relative_to(args.output)),
                     "materials": str(materials_path.relative_to(args.output)),
                     "speech_sources": speech_paths, "sample_rate_hz": 48000, "channels": 8,
                     "channel_8_policy": "zero", "source_positions_m": sources,
                     "microphone_positions_m": mics.tolist(), "array_local_geometry_m": local_mics.tolist(),
                     "timestamp_start_s": 0.0, "truth": truth, "normalization_gain": gain,
                     "rir_backend": backend_stats, "impairments": impairments, "odas_estimate": odas_estimate}
            manifest.write(json.dumps(entry) + "\n")
            manifest.flush()


if __name__ == "__main__":
    main()
