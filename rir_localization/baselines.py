#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rir_localization.dataset import preprocess_rir
from rir_localization.utils import (
    load_config, load_measurement_rir, localization_metrics, read_csv, write_json,
)


def rir_features(signal: np.ndarray, windows: int = 12) -> np.ndarray:
    """Per channel: log window RMS, log peak, normalized peak index, log global RMS."""
    if signal.ndim != 2:
        raise ValueError(f"feature input 必须 [C,T]：{signal.shape}")
    chunks = np.array_split(signal, windows, axis=1)
    window_rms = [np.sqrt(np.mean(chunk.astype(np.float64) ** 2, axis=1)) for chunk in chunks]
    peak = np.max(np.abs(signal), axis=1)
    peak_index = np.argmax(np.abs(signal), axis=1) / max(1, signal.shape[1] - 1)
    global_rms = np.sqrt(np.mean(signal.astype(np.float64) ** 2, axis=1))
    values = np.stack((*window_rms, peak, peak_index, global_rms), axis=1)
    values[:, :windows] = np.log10(values[:, :windows] + 1e-12)
    values[:, windows] = np.log10(values[:, windows] + 1e-12)
    values[:, windows + 2] = np.log10(values[:, windows + 2] + 1e-12)
    return values.reshape(-1).astype(np.float32)


def _matrix(rows: Sequence[dict[str, str]], data_cfg: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    features: list[np.ndarray] = []; positions: list[list[float]] = []
    channels = list(map(int, data_cfg["channels"])); rate = int(data_cfg["sample_rate"])
    samples = round(rate * float(data_cfg["rir_duration_ms"]) / 1000.0)
    for row in rows:
        array, _, _, _ = load_measurement_rir(row["measurement_dir"], int(row["rir_channels"]))
        signal = preprocess_rir(array, channels=channels, target_samples=samples,
                                normalization=str(data_cfg["normalization"]))
        features.append(rir_features(signal)); positions.append([float(row["rx_x"]), float(row["rx_y"])])
    return np.stack(features), np.asarray(positions, dtype=np.float32)


def run_knn(train_csv: Path, test_csv: Path, config_path: Path, output: Path, k: int) -> dict[str, Any]:
    config = load_config(config_path); train_rows, test_rows = read_csv(train_csv), read_csv(test_csv)
    if not train_rows or not test_rows: raise ValueError("KNN train/test split 不能为空")
    train_x, train_y = _matrix(train_rows, config["data"]); test_x, test_y = _matrix(test_rows, config["data"])
    mean, std = train_x.mean(axis=0), np.maximum(train_x.std(axis=0), 1e-6)
    train_z, test_z = (train_x - mean) / std, (test_x - mean) / std
    k = min(max(1, k), len(train_rows)); predictions: list[np.ndarray] = []
    neighbors: list[list[int]] = []
    for item in test_z:
        distances = np.linalg.norm(train_z - item, axis=1); indices = np.argsort(distances)[:k]
        weights = 1.0 / np.maximum(distances[indices], 1e-8)
        predictions.append(np.average(train_y[indices], axis=0, weights=weights)); neighbors.append(indices.tolist())
    predicted = np.stack(predictions); metrics = localization_metrics(predicted, test_y)
    output.mkdir(parents=True, exist_ok=True)
    np.savez(output / "knn_model.npz", feature_mean=mean, feature_std=std, train_features=train_z,
             train_positions=train_y, k=k)
    fields = ["session_id", "measurement_id", "gt_x", "gt_y", "pred_x", "pred_y", "error_m", "neighbor_indices"]
    with (output / "knn_predictions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for row, gt, pred, indices in zip(test_rows, test_y, predicted, neighbors):
            writer.writerow({"session_id": row["session_id"], "measurement_id": row["measurement_id"],
                             "gt_x": gt[0], "gt_y": gt[1], "pred_x": pred[0], "pred_y": pred[1],
                             "error_m": float(np.linalg.norm(pred - gt)), "neighbor_indices": str(indices)})
    report = {**metrics, "baseline": "KNN", "k": k, "distance": "Euclidean on train-standardized features",
              "features": "Per-channel 12-bin log RMS + log peak + normalized peak index + log global RMS",
              "normalization": "Feature mean/std fit on train split only",
              "paper_claim": "Independent engineering baseline; not claimed as the paper's undisclosed KNN."}
    write_json(output / "knn_metrics.json", report); return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="低维 RIR 特征 KNN 定位 baseline")
    result.add_argument("--config", required=True, type=Path); result.add_argument("--train", required=True, type=Path)
    result.add_argument("--test", required=True, type=Path); result.add_argument("--output", required=True, type=Path)
    result.add_argument("--k", type=int, default=3); return result


def main() -> int:
    args = parser().parse_args()
    try: metrics = run_knn(args.train, args.test, args.config, args.output, args.k)
    except Exception as exc: print(f"ERROR: KNN failed: {exc}", file=sys.stderr); return 2
    print("=== KNN baseline ==="); print(f"Median error: {metrics['median_error_m']:.6f} m"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
