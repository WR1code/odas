#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import torch
from torch.utils.data import DataLoader

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rir_localization.checkpoint import load_checkpoint
from rir_localization.dataset import CoordinateTransform, GridTransform, RIRDataset
from rir_localization.model import build_model
from rir_localization.utils import localization_metrics, write_json


def _device(requested: str) -> torch.device:
    if requested == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(requested)


def evaluate_checkpoint(checkpoint_path: Path, *, split: str = "test", output: Path | None = None,
                        batch_size: int = 32, device_name: str = "auto") -> dict[str, Any]:
    device = _device(device_name)
    checkpoint = load_checkpoint(checkpoint_path, device)
    config = checkpoint["config"]
    data_cfg, model_cfg = config["data"], config["model"]
    task = str(config.get("task", "regression"))
    split_paths = checkpoint.get("split_paths", {})
    if split not in split_paths:
        raise ValueError(f"checkpoint 没有 split={split}；可用 {sorted(split_paths)}")
    csv_path = Path(split_paths[split])
    coordinate = CoordinateTransform.from_dict(checkpoint["coordinate_transform"])
    grid = GridTransform.from_dict(checkpoint["grid_transform"]) if checkpoint.get("grid_transform") else None
    dataset = RIRDataset(
        csv_path, channels=checkpoint["input_channels"], sample_rate=int(checkpoint["sample_rate"]),
        rir_duration_ms=float(checkpoint["rir_duration_ms"]), normalization=checkpoint["normalization"],
        coordinate_transform=coordinate, task=task, grid_transform=grid,
        use_tof_feature=bool(model_cfg.get("use_tof_feature", False)),
    )
    if not len(dataset):
        raise ValueError(f"{split} split 为空：{csv_path}")
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)
    model = build_model(model_cfg, in_channels=len(checkpoint["input_channels"]), task=task,
                        num_classes=None if grid is None else grid.num_classes).to(device)
    model.load_state_dict(checkpoint["model_state_dict"]); model.eval()
    predicted: list[np.ndarray] = []; truth: list[np.ndarray] = []
    identities: list[tuple[str, str]] = []
    with torch.no_grad():
        for batch in loader:
            rir = batch["rir"].to(device)
            tof = batch["tof"].to(device) if model.use_tof_feature else None
            output_tensor = model(rir, tof)
            if task == "regression":
                positions = coordinate.inverse(output_tensor).cpu().numpy()
            else:
                assert grid is not None
                positions = grid.decode_tensor(output_tensor.argmax(dim=1).cpu()).numpy()
            predicted.append(positions); truth.append(batch["position_m"].numpy())
            identities.extend(zip(list(batch["session_id"]), list(batch["measurement_id"])))
    predictions = np.concatenate(predicted); targets = np.concatenate(truth)
    errors = np.linalg.norm(predictions - targets, axis=1)
    metrics = localization_metrics(predictions, targets)
    metrics.update({"task": task, "split": split, "checkpoint": str(checkpoint_path.resolve())})
    destination = output if output is not None else checkpoint_path.resolve().parent.parent
    destination.mkdir(parents=True, exist_ok=True)
    prediction_rows = [{
        "session_id": session_id, "measurement_id": measurement_id,
        "gt_x": float(gt[0]), "gt_y": float(gt[1]), "pred_x": float(pred[0]),
        "pred_y": float(pred[1]), "error_m": float(error),
    } for (session_id, measurement_id), gt, pred, error in zip(identities, targets, predictions, errors)]
    with (destination / "predictions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(prediction_rows[0])); writer.writeheader(); writer.writerows(prediction_rows)
    write_json(destination / "metrics.json", metrics)

    ordered = np.sort(errors); cdf = np.arange(1, len(errors) + 1) / len(errors)
    fig, ax = plt.subplots(figsize=(6, 4.5)); ax.step(ordered, cdf, where="post")
    ax.set(xlabel="Localization error (m)", ylabel="CDF", title=f"Localization CDF ({split})")
    ax.set_ylim(0, 1.02); ax.grid(alpha=0.3); fig.tight_layout()
    fig.savefig(destination / "localization_cdf.png", dpi=180); plt.close(fig)
    fig, ax = plt.subplots(figsize=(6.5, 6)); ax.scatter(targets[:, 0], targets[:, 1], label="GT")
    ax.scatter(predictions[:, 0], predictions[:, 1], marker="x", label="Prediction")
    for gt, pred in zip(targets, predictions): ax.plot([gt[0], pred[0]], [gt[1], pred[1]], color="gray", alpha=0.5)
    ax.set(xlabel="x (m)", ylabel="y (m)", title="Ground truth vs prediction"); ax.axis("equal")
    ax.grid(alpha=0.3); ax.legend(); fig.tight_layout(); fig.savefig(destination / "prediction_scatter.png", dpi=180); plt.close(fig)
    fig, ax = plt.subplots(figsize=(6, 4.5)); ax.hist(errors, bins=min(20, max(1, len(errors))), edgecolor="black")
    ax.set(xlabel="Localization error (m)", ylabel="Count", title="Localization error histogram")
    ax.grid(alpha=0.3); fig.tight_layout(); fig.savefig(destination / "error_histogram.png", dpi=180); plt.close(fig)
    fig, ax = plt.subplots(figsize=(6.5, 6)); scatter = ax.scatter(targets[:, 0], targets[:, 1], c=errors, cmap="viridis", s=60)
    ax.set(xlabel="GT x (m)", ylabel="GT y (m)", title="Spatial error map"); ax.axis("equal"); ax.grid(alpha=0.3)
    fig.colorbar(scatter, ax=ax, label="Error (m)"); fig.tight_layout()
    fig.savefig(destination / "spatial_error_map.png", dpi=180); plt.close(fig)
    return metrics


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="评估 RIR 定位 checkpoint")
    result.add_argument("--checkpoint", required=True, type=Path)
    result.add_argument("--split", choices=("train", "val", "test"), default="test")
    result.add_argument("--output", type=Path)
    result.add_argument("--batch-size", type=int, default=32)
    result.add_argument("--device", default="auto")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        metrics = evaluate_checkpoint(args.checkpoint, split=args.split, output=args.output,
                                      batch_size=args.batch_size, device_name=args.device)
    except Exception as exc:
        print(f"ERROR: evaluation failed: {exc}", file=sys.stderr); return 2
    print("=== Localization Evaluation ===")
    for key, value in metrics.items():
        if key.endswith("_m"): print(f"{key}: {value:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
