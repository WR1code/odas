#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rir_localization.utils import (
    PASS_VALUES, discover_measurements, discover_sessions, failure_reason, load_rir,
    measurement_status, optional_float, percentile_summary, position_from_pose,
    read_json, session_channel_count, write_json,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="审计 AV-Twin continuous session RIR 数据")
    result.add_argument("--input", required=True, type=Path, help="采集输出根目录")
    result.add_argument("--output", type=Path, default=Path("inspection_report"), help="报告输出目录")
    result.add_argument("--speed-of-sound-mps", type=float, default=343.0)
    result.add_argument("--near-duplicate-distance-m", type=float, default=0.05)
    result.add_argument("--inactive-epsilon", type=float, default=1e-12)
    return result


def _safe_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return read_json(path), None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, str(exc)


def _plot_hist(values: list[float], path: Path, xlabel: str, title: str) -> None:
    if not values:
        return
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.hist(values, bins=min(30, max(5, int(math.sqrt(len(values)) * 2))), edgecolor="black")
    ax.set(xlabel=xlabel, ylabel="Count", title=title)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def inspect(input_root: Path, output: Path, speed: float, near_threshold: float,
            inactive_epsilon: float) -> dict[str, Any]:
    input_root = input_root.resolve()
    if not input_root.is_dir():
        raise FileNotFoundError(f"输入目录不存在：{input_root}")
    output.mkdir(parents=True, exist_ok=True)
    sessions = discover_sessions(input_root)
    measurements = discover_measurements(input_root)
    legacy = [path for path in input_root.rglob("result.json") if path.parent not in measurements]
    session_meta: dict[Path, dict[str, Any]] = {}
    for directory in sessions:
        value, _ = _safe_json(directory / "session.json")
        session_meta[directory] = value or {}

    counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    dtypes: Counter[str] = Counter()
    sample_rates: Counter[str] = Counter()
    duration_ms_values: list[float] = []
    tof_values: list[float] = []
    direct_samples: list[float] = []
    geometry_values: list[float] = []
    tof_distance_values: list[float] = []
    tof_geometry_errors: list[float] = []
    usable_points: list[dict[str, Any]] = []
    per_channel: dict[int, dict[str, list[float] | int]] = defaultdict(
        lambda: {"peaks": [], "rms": [], "zero_ratios": [], "inactive_arrays": 0,
                 "usable_peaks": [], "nan_arrays": 0, "inf_arrays": 0,
                 "clipped_samples": 0, "total_samples": 0}
    )
    time_references: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    inspected_rows: list[dict[str, Any]] = []

    for measurement_dir in measurements:
        counts["measurements_total"] += 1
        metadata, json_error = _safe_json(measurement_dir / "result.json")
        if metadata is None:
            counts["bad_json"] += 1
            inspected_rows.append({"path": str(measurement_dir), "usable": False, "reason": json_error})
            continue
        status = measurement_status(metadata)
        status_counts[status] += 1
        directory_id = measurement_dir.name.lstrip("0") or "0"
        metadata_id = str(metadata.get("measurement_id", ""))
        id_matches = metadata_id == directory_id
        counts["measurement_id_mismatch"] += int(not id_matches)
        reason = failure_reason(metadata)
        if reason:
            failure_reasons[reason] += 1
        rx = position_from_pose(metadata, "rx_pose")
        tx = position_from_pose(metadata, "tx_pose")
        raw_rx = metadata.get("rx_pose")
        if raw_rx is None:
            counts["missing_pose"] += 1
        elif rx is None:
            counts["invalid_pose"] += 1
        sample_rate = optional_float(metadata.get("sample_rate"))
        if sample_rate:
            sample_rates[str(int(sample_rate))] += 1
        rir_meta = metadata.get("rir") if isinstance(metadata.get("rir"), dict) else {}
        duration = optional_float(rir_meta.get("duration_ms"))
        if duration is not None:
            duration_ms_values.append(duration)
        direct = optional_float(rir_meta.get("direct_arrival_index"))
        if direct is not None:
            direct_samples.append(direct)
        if rir_meta.get("time_reference"):
            time_references[str(rir_meta["time_reference"])] += 1
        tof_meta = metadata.get("tof") if isinstance(metadata.get("tof"), dict) else {}
        tof = optional_float(tof_meta.get("tof_seconds"))
        if tof is not None:
            tof_values.append(tof)
        if tx is not None and rx is not None and tof is not None:
            distance = math.dist(tx, rx)
            tof_distance = speed * tof
            geometry_values.append(distance)
            tof_distance_values.append(tof_distance)
            tof_geometry_errors.append(tof_distance - distance)

        rir_path = measurement_dir / "rir_float32.npy"
        array: np.ndarray | None = None
        array_ok = False
        layout = ""
        rir_reason = ""
        if not rir_path.is_file():
            counts["bad_npy"] += 1
            rir_reason = "missing_npy"
        else:
            session_dir = measurement_dir.parent.parent
            expected = session_channel_count(session_meta.get(session_dir, {}))
            try:
                original = np.load(rir_path, allow_pickle=False, mmap_mode="r")
                shapes[str(list(original.shape))] += 1
                dtypes[str(original.dtype)] += 1
                array, layout = load_rir(rir_path, expected)
                if original.ndim != 2:
                    raise ValueError(f"invalid ndim {original.ndim}")
                nan_count = int(np.isnan(array).sum())
                inf_count = int(np.isinf(array).sum())
                counts["nan"] += int(nan_count > 0)
                counts["inf"] += int(inf_count > 0)
                for channel, signal in enumerate(array):
                    info = per_channel[channel]
                    finite = signal[np.isfinite(signal)]
                    info["nan_arrays"] = int(info["nan_arrays"]) + int(np.isnan(signal).any())
                    info["inf_arrays"] = int(info["inf_arrays"]) + int(np.isinf(signal).any())
                    info["total_samples"] = int(info["total_samples"]) + int(signal.size)
                    info["clipped_samples"] = int(info["clipped_samples"]) + int(np.sum(np.abs(finite) >= 1.0))
                    peak = float(np.max(np.abs(finite))) if finite.size else float("nan")
                    rms = float(np.sqrt(np.mean(finite.astype(np.float64) ** 2))) if finite.size else float("nan")
                    zero_ratio = float(np.mean(finite == 0.0)) if finite.size else float("nan")
                    cast_peaks = info["peaks"]; assert isinstance(cast_peaks, list); cast_peaks.append(peak)
                    cast_rms = info["rms"]; assert isinstance(cast_rms, list); cast_rms.append(rms)
                    cast_zero = info["zero_ratios"]; assert isinstance(cast_zero, list); cast_zero.append(zero_ratio)
                    info["inactive_arrays"] = int(info["inactive_arrays"]) + int(peak <= inactive_epsilon)
                array_ok = nan_count == 0 and inf_count == 0
            except (OSError, ValueError, EOFError) as exc:
                counts["bad_npy"] += 1
                counts["invalid_shape"] += int("shape" in str(exc) or "二维" in str(exc) or "axis" in str(exc))
                rir_reason = f"bad_npy:{exc}"

        usable = status in PASS_VALUES and rx is not None and array_ok and id_matches
        if usable:
            counts["usable_samples"] += 1
            assert array is not None
            for channel, signal in enumerate(array):
                usable_peaks = per_channel[channel]["usable_peaks"]
                assert isinstance(usable_peaks, list)
                usable_peaks.append(float(np.max(np.abs(signal))))
            usable_points.append({
                "session_id": metadata.get("session_id", measurement_dir.parent.parent.name),
                "measurement_id": metadata.get("measurement_id", measurement_dir.name),
                "timestamp": metadata.get("wall_clock_timestamp", ""), "rx": rx, "tx": tx,
            })
        inspected_rows.append({
            "path": str(measurement_dir), "session_id": metadata.get("session_id"),
            "measurement_id": metadata.get("measurement_id"), "status": status, "failure_reason": reason,
            "has_rx_pose": rx is not None, "measurement_id_matches_directory": id_matches,
            "npy_ok": array_ok, "rir_layout": layout,
            "usable": usable, "reason": rir_reason,
        })

    channel_stats: dict[str, Any] = {}
    for channel, info in sorted(per_channel.items()):
        peaks = info["peaks"]; rms = info["rms"]; zero_ratios = info["zero_ratios"]
        usable_peaks = info["usable_peaks"]
        assert isinstance(peaks, list) and isinstance(rms, list) and isinstance(zero_ratios, list)
        assert isinstance(usable_peaks, list)
        inactive_count = int(info["inactive_arrays"])
        observed = len(peaks)
        channel_stats[str(channel)] = {
            "arrays_observed": observed, "peak": percentile_summary(peaks),
            "rms": percentile_summary(rms), "zero_ratio": percentile_summary(zero_ratios),
            "nan_arrays": int(info["nan_arrays"]), "inf_arrays": int(info["inf_arrays"]),
            "clipped_samples": int(info["clipped_samples"]), "total_samples": int(info["total_samples"]),
            "inactive_zero_arrays": inactive_count,
            "appears_inactive_zero": bool(observed and inactive_count / observed >= 0.95),
            "usable_arrays_observed": len(usable_peaks),
            "usable_inactive_zero_arrays": int(sum(value <= inactive_epsilon for value in usable_peaks)),
            "usable_peak": percentile_summary(usable_peaks),
        }

    points = np.asarray([item["rx"][:2] for item in usable_points], dtype=np.float64)
    step_distances: list[float] = []
    for first, second in zip(points[:-1], points[1:]):
        step_distances.append(float(np.linalg.norm(second - first)))
    near_ratio = None
    if len(points) >= 2:
        distances = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
        np.fill_diagonal(distances, np.inf)
        near_ratio = float(np.mean(np.min(distances, axis=1) <= near_threshold))
    spatial = {
        "valid_samples": len(points),
        "x_min": float(points[:, 0].min()) if len(points) else None,
        "x_max": float(points[:, 0].max()) if len(points) else None,
        "y_min": float(points[:, 1].min()) if len(points) else None,
        "y_max": float(points[:, 1].max()) if len(points) else None,
        "bounding_box_area_m2": float(np.ptp(points[:, 0]) * np.ptp(points[:, 1])) if len(points) else None,
        "step_distance_m": percentile_summary(step_distances),
        "near_duplicate_distance_m": near_threshold, "near_duplicate_position_ratio": near_ratio,
    }
    if len(points):
        fig, ax = plt.subplots(figsize=(6.5, 6))
        ax.plot(points[:, 0], points[:, 1], "o-", label="Rx trajectory", alpha=0.8)
        tx_points = np.asarray([item["tx"][:2] for item in usable_points if item["tx"] is not None])
        if len(tx_points):
            ax.scatter(tx_points[:, 0], tx_points[:, 1], marker="*", s=140, label="tx_pose metadata")
        ax.set(xlabel="x (m)", ylabel="y (m)", title="Dataset spatial coverage")
        ax.axis("equal"); ax.grid(alpha=0.3); ax.legend()
        fig.tight_layout(); fig.savefig(output / "dataset_spatial_coverage.png", dpi=180); plt.close(fig)
    if geometry_values:
        fig, ax = plt.subplots(figsize=(6, 5))
        ax.scatter(geometry_values, tof_distance_values)
        extent = max(geometry_values + tof_distance_values)
        ax.plot([0, extent], [0, extent], "--", color="gray", label="ideal y=x")
        ax.set(xlabel="Geometry distance from tx_pose/rx_pose (m)", ylabel="ToF distance (m)",
               title="ToF vs metadata geometry distance")
        ax.grid(alpha=0.3); ax.legend(); fig.tight_layout()
        fig.savefig(output / "tof_vs_geometry_distance.png", dpi=180); plt.close(fig)
        _plot_hist(tof_geometry_errors, output / "tof_geometry_error_histogram.png",
                   "ToF distance - geometry distance (m)", "ToF/geometry discrepancy")
    _plot_hist(duration_ms_values, output / "rir_duration_distribution.png", "RIR duration (ms)", "RIR durations")
    _plot_hist(direct_samples, output / "direct_peak_sample_distribution.png", "Direct arrival sample", "Direct arrival index")
    _plot_hist(tof_values, output / "tof_distribution.png", "ToF (s)", "ToF distribution")

    report = {
        "input_root": str(input_root), "sessions": len(sessions),
        "measurements_total": counts["measurements_total"], "status_distribution": dict(status_counts),
        "pass": sum(value for key, value in status_counts.items() if key in PASS_VALUES),
        "fail": status_counts.get("FAIL", 0), "missing_pose": counts["missing_pose"],
        "invalid_pose": counts["invalid_pose"], "bad_json": counts["bad_json"],
        "bad_npy": counts["bad_npy"], "nan_arrays": counts["nan"], "inf_arrays": counts["inf"],
        "invalid_shape": counts["invalid_shape"], "usable_samples": counts["usable_samples"],
        "measurement_id_mismatch": counts["measurement_id_mismatch"],
        "legacy_result_json_excluded": len(legacy), "sample_rates_hz": dict(sample_rates),
        "original_shape_distribution": dict(shapes), "dtype_distribution": dict(dtypes),
        "duration_ms": percentile_summary(duration_ms_values),
        "direct_arrival_sample": percentile_summary(direct_samples),
        "time_reference_distribution": dict(time_references), "tof_seconds": percentile_summary(tof_values),
        "geometry_distance_m": percentile_summary(geometry_values),
        "tof_distance_m": percentile_summary(tof_distance_values),
        "tof_minus_geometry_m": percentile_summary(tof_geometry_errors),
        "speed_of_sound_mps": speed, "channels": channel_stats, "spatial_coverage": spatial,
        "failure_reason_distribution": dict(failure_reasons), "measurements": inspected_rows,
        "notes": [
            "Geometry comparison uses the metadata fields literally; verify that tx_pose and rx_pose represent the two acoustic endpoints.",
            "Legacy one-shot result directories are reported but excluded from continuous-session counts.",
        ],
    }
    write_json(output / "inspection_report.json", report)
    return report


def main() -> int:
    args = parser().parse_args()
    try:
        report = inspect(args.input, args.output, args.speed_of_sound_mps,
                         args.near_duplicate_distance_m, args.inactive_epsilon)
    except Exception as exc:
        print(f"ERROR: dataset inspection failed: {exc}", file=sys.stderr)
        return 2
    print("=== RIR Dataset Inspection ===")
    for key in ("sessions", "measurements_total", "pass", "fail", "missing_pose", "invalid_pose",
                "bad_npy", "nan_arrays", "inf_arrays", "invalid_shape", "measurement_id_mismatch",
                "usable_samples"):
        print(f"{key}: {report[key]}")
    print(f"shape distribution: {report['original_shape_distribution']}")
    print(f"dtype distribution: {report['dtype_distribution']}")
    for channel, info in report["channels"].items():
        state = " inactive_zero" if info["appears_inactive_zero"] else ""
        print(f"CH{channel}: inactive {info['inactive_zero_arrays']}/{info['arrays_observed']}{state}")
    print(f"Report: {(args.output / 'inspection_report.json').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
