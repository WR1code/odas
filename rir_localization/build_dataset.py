#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rir_localization.utils import (
    PASS_VALUES, discover_measurements, failure_reason, load_measurement_rir,
    measurement_status, optional_float, percentile_summary, position_from_pose,
    read_json, session_channel_count, valid_position, write_csv, write_json,
)


DATASET_FIELDS = [
    "session_id", "measurement_id", "measurement_dir", "rir_path", "rir_source", "metadata_path",
    "status", "rx_x", "rx_y", "rx_z", "rx_frame", "rx_child_frame", "rx_pose_timestamp_ns",
    "tx_x", "tx_y", "tx_z", "tx_frame", "tx_child_frame", "tx_pose_timestamp_ns",
    "tof_seconds", "tof_distance_m", "sample_rate", "rir_samples", "rir_channels", "rir_layout",
    "rir_dtype", "rir_duration_ms", "direct_arrival_sample", "time_reference", "timestamp",
    "rir_valid_channels", "overall_policy",
]
EXCLUDED_FIELDS = [
    "session_id", "measurement_id", "reason", "original_status", "failure_reason", "metadata_path",
]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="从 AV-Twin session metadata 构建 RIR 定位索引")
    result.add_argument("--input", required=True, type=Path, help="采集输出根目录")
    result.add_argument("--output", required=True, type=Path, help="新 dataset 输出目录")
    result.add_argument("--speed-of-sound-mps", type=float, default=343.0)
    result.add_argument("--inactive-epsilon", type=float, default=1e-12)
    return result


def _pose_fields(metadata: dict[str, Any], key: str, prefix: str) -> dict[str, Any]:
    pose = metadata.get(key)
    position = position_from_pose(metadata, key)
    if not isinstance(pose, dict) or position is None:
        return {f"{prefix}_{axis}": "" for axis in "xyz"} | {
            f"{prefix}_frame": "", f"{prefix}_child_frame": "", f"{prefix}_pose_timestamp_ns": ""
        }
    return {
        f"{prefix}_x": position[0], f"{prefix}_y": position[1], f"{prefix}_z": position[2],
        f"{prefix}_frame": pose.get("frame_id", ""),
        f"{prefix}_child_frame": pose.get("child_frame_id", ""),
        f"{prefix}_pose_timestamp_ns": pose.get("source_pose_timestamp_ns", ""),
    }


def _hist(values: list[float], path: Path, xlabel: str) -> None:
    if not values:
        return
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    ax.hist(values, bins=min(25, max(5, round(math.sqrt(len(values)) * 2))), edgecolor="black")
    ax.set(xlabel=xlabel, ylabel="Count"); ax.grid(alpha=0.25)
    fig.tight_layout(); fig.savefig(path, dpi=160); plt.close(fig)


def build(input_root: Path, output: Path, speed: float, inactive_epsilon: float) -> dict[str, Any]:
    input_root = input_root.resolve()
    if not input_root.is_dir():
        raise FileNotFoundError(f"输入目录不存在：{input_root}")
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    exclusion_counts: Counter[str] = Counter()
    layout_counts: Counter[str] = Counter()
    shape_counts: Counter[str] = Counter()
    dtype_counts: Counter[str] = Counter()
    session_cache: dict[Path, dict[str, Any]] = {}
    peaks: list[float] = []
    rms_values: list[float] = []
    lengths: list[float] = []
    tof_values: list[float] = []

    for directory in discover_measurements(input_root):
        metadata_path = directory / "result.json"
        try:
            metadata = read_json(metadata_path)
        except Exception as exc:
            excluded.append({"session_id": directory.parent.parent.name, "measurement_id": directory.name,
                             "reason": "bad_json", "original_status": "UNKNOWN",
                             "failure_reason": str(exc), "metadata_path": str(metadata_path.resolve())})
            exclusion_counts["bad_json"] += 1
            continue
        status = measurement_status(metadata)
        session_id = str(metadata.get("session_id") or directory.parent.parent.name)
        measurement_id = metadata.get("measurement_id", directory.name)
        reasons: list[str] = []
        if str(measurement_id) != (directory.name.lstrip("0") or "0"):
            reasons.append("measurement_id_mismatch")
        if status not in PASS_VALUES:
            reasons.append("status_not_pass")
        rx = position_from_pose(metadata, "rx_pose")
        if metadata.get("rx_pose") is None:
            reasons.append("missing_rx_pose")
        elif rx is None:
            reasons.append("invalid_rx_pose")
        session_dir = directory.parent.parent
        if session_dir not in session_cache:
            try:
                session_cache[session_dir] = read_json(session_dir / "session.json")
            except Exception:
                session_cache[session_dir] = {}
        expected_channels = session_channel_count(session_cache[session_dir])
        array: np.ndarray | None = None
        rir_path: Path | None = None
        source = ""
        layout = ""
        try:
            array, layout, rir_path, source = load_measurement_rir(directory, expected_channels)
            if not np.isfinite(array).all():
                reasons.append("rir_nan_or_inf")
            if array.ndim != 2 or min(array.shape) < 1:
                reasons.append("invalid_rir_shape")
            if not np.any(np.max(np.abs(array), axis=1) > inactive_epsilon):
                reasons.append("all_rir_channels_inactive")
        except (OSError, ValueError, EOFError) as exc:
            reasons.append(f"bad_rir:{exc}")
        quality = metadata.get("quality") if isinstance(metadata.get("quality"), dict) else {}
        if quality.get("rir_pass") is False and status in PASS_VALUES:
            reasons.append("metadata_rir_quality_fail")
        if reasons:
            simple_reasons = [item.split(":", 1)[0] for item in reasons]
            exclusion_counts.update(simple_reasons)
            excluded.append({
                "session_id": session_id, "measurement_id": measurement_id, "reason": ";".join(reasons),
                "original_status": status, "failure_reason": failure_reason(metadata),
                "metadata_path": str(metadata_path.resolve()),
            })
            continue
        assert array is not None and rir_path is not None and rx is not None
        tx = position_from_pose(metadata, "tx_pose")
        tof_meta = metadata.get("tof") if isinstance(metadata.get("tof"), dict) else {}
        tof = optional_float(tof_meta.get("tof_seconds"))
        sample_rate = optional_float(metadata.get("sample_rate"))
        if sample_rate is None:
            raise ValueError(f"PASS 样本缺少 sample_rate：{metadata_path}")
        rir_meta = metadata.get("rir") if isinstance(metadata.get("rir"), dict) else {}
        row: dict[str, Any] = {
            "session_id": session_id, "measurement_id": measurement_id,
            "measurement_dir": str(directory.resolve()), "rir_path": str(rir_path), "rir_source": source,
            "metadata_path": str(metadata_path.resolve()), "status": status,
            "tof_seconds": "" if tof is None else tof,
            "tof_distance_m": "" if tof is None else tof * speed,
            "sample_rate": int(sample_rate), "rir_samples": int(array.shape[1]),
            "rir_channels": int(array.shape[0]), "rir_layout": layout, "rir_dtype": str(array.dtype),
            "rir_duration_ms": optional_float(rir_meta.get("duration_ms")) or array.shape[1] * 1000.0 / sample_rate,
            "direct_arrival_sample": rir_meta.get("direct_arrival_index", ""),
            "time_reference": rir_meta.get("time_reference", ""),
            "timestamp": metadata.get("wall_clock_timestamp", ""),
            "rir_valid_channels": quality.get("rir_valid_channels", ""),
            "overall_policy": quality.get("overall_policy", ""),
        }
        row.update(_pose_fields(metadata, "rx_pose", "rx"))
        row.update(_pose_fields(metadata, "tx_pose", "tx"))
        rows.append(row)
        layout_counts[layout] += 1; shape_counts[str(list(array.shape))] += 1; dtype_counts[str(array.dtype)] += 1
        peaks.extend(np.max(np.abs(array), axis=1).tolist())
        rms_values.extend(np.sqrt(np.mean(array.astype(np.float64) ** 2, axis=1)).tolist())
        lengths.append(float(array.shape[1]))
        if tof is not None:
            tof_values.append(tof)

    write_csv(output / "dataset.csv", rows, DATASET_FIELDS)
    write_csv(output / "excluded_samples.csv", excluded, EXCLUDED_FIELDS)
    points = np.asarray([[float(row["rx_x"]), float(row["rx_y"])] for row in rows], dtype=np.float64)
    if len(points):
        fig, ax = plt.subplots(figsize=(6.5, 6))
        ax.plot(points[:, 0], points[:, 1], "o-", label="Rx")
        tx = np.asarray([[float(row["tx_x"]), float(row["tx_y"])] for row in rows
                         if row["tx_x"] != "" and row["tx_y"] != ""])
        if len(tx): ax.scatter(tx[:, 0], tx[:, 1], marker="*", s=140, label="tx_pose metadata")
        ax.set(xlabel="x (m)", ylabel="y (m)", title="Usable dataset spatial coverage")
        ax.axis("equal"); ax.grid(alpha=0.3); ax.legend(); fig.tight_layout()
        fig.savefig(output / "dataset_spatial_coverage.png", dpi=180); plt.close(fig)
    _hist(lengths, output / "rir_length_distribution.png", "RIR samples")
    _hist(peaks, output / "rir_peak_distribution.png", "Per-channel |peak|")
    _hist(rms_values, output / "rir_rms_distribution.png", "Per-channel RMS")
    _hist(tof_values, output / "tof_distribution.png", "ToF (s)")
    stats = {
        "input_root": str(input_root), "dataset_csv": str((output / "dataset.csv").resolve()),
        "measurements_scanned": len(rows) + len(excluded), "usable_samples": len(rows),
        "excluded_samples": len(excluded), "exclusion_reason_counts": dict(exclusion_counts),
        "sessions_usable": len({row["session_id"] for row in rows}),
        "canonical_shape_distribution": dict(shape_counts), "source_layout_distribution": dict(layout_counts),
        "dtype_distribution": dict(dtype_counts), "sample_rate_distribution": dict(Counter(str(row["sample_rate"]) for row in rows)),
        "rir_samples": percentile_summary(lengths), "per_channel_peak": percentile_summary(peaks),
        "per_channel_rms": percentile_summary(rms_values), "tof_seconds": percentile_summary(tof_values),
        "speed_of_sound_mps": speed,
        "coordinates": {
            "rx_field": "rx_pose.position_m", "tx_field": "tx_pose.position_m", "unit": "m",
            "frames": sorted({str(row["rx_frame"]) for row in rows if row["rx_frame"]}),
        },
        "does_not_copy_raw_rir": True,
    }
    write_json(output / "dataset_stats.json", stats)
    return stats


def main() -> int:
    args = parser().parse_args()
    try:
        stats = build(args.input, args.output, args.speed_of_sound_mps, args.inactive_epsilon)
    except Exception as exc:
        print(f"ERROR: dataset build failed: {exc}", file=sys.stderr)
        return 2
    print("=== RIR Dataset Build ===")
    print(f"Measurements scanned: {stats['measurements_scanned']}")
    print(f"Usable samples: {stats['usable_samples']}")
    print(f"Excluded samples: {stats['excluded_samples']}")
    print(f"Dataset: {stats['dataset_csv']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
