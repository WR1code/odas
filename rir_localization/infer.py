#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import torch

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rir_localization.checkpoint import load_checkpoint
from rir_localization.dataset import CoordinateTransform, GridTransform, preprocess_rir
from rir_localization.model import build_model, topk_location_probabilities
from rir_localization.utils import load_measurement_rir, load_rir, optional_float, read_json


def predict_single(checkpoint_path: Path, rir_path: Path, *, measurement_dir: Path | None = None,
                   tof_seconds: float | None = None, device_name: str = "auto") -> dict[str, Any]:
    device = torch.device("cuda" if device_name == "auto" and torch.cuda.is_available() else
                          "cpu" if device_name == "auto" else device_name)
    checkpoint = load_checkpoint(checkpoint_path, device)
    config = checkpoint["config"]; task = str(config.get("task", "regression")); model_cfg = config["model"]
    grid = GridTransform.from_dict(checkpoint["grid_transform"]) if checkpoint.get("grid_transform") else None
    if measurement_dir is not None:
        array, _, _, _ = load_measurement_rir(measurement_dir, checkpoint.get("rir_total_channels"))
        metadata_path = measurement_dir / "result.json"
        if tof_seconds is None and metadata_path.is_file():
            metadata = read_json(metadata_path); tof_meta = metadata.get("tof", {})
            if isinstance(tof_meta, dict): tof_seconds = optional_float(tof_meta.get("tof_seconds"))
    else:
        array, _ = load_rir(rir_path, checkpoint.get("rir_total_channels"))
    processed = preprocess_rir(
        array, channels=checkpoint["input_channels"],
        target_samples=round(checkpoint["sample_rate"] * checkpoint["rir_duration_ms"] / 1000.0),
        normalization=checkpoint["normalization"],
    )
    model = build_model(model_cfg, in_channels=len(checkpoint["input_channels"]), task=task,
                        num_classes=None if grid is None else grid.num_classes).to(device)
    model.load_state_dict(checkpoint["model_state_dict"]); model.eval()
    if model.use_tof_feature and tof_seconds is None:
        raise ValueError("checkpoint 需要 ToF；请用 --measurement 自动读取或提供 --tof-seconds")
    rir_tensor = torch.from_numpy(processed)[None].to(device)
    tof_tensor = torch.tensor([[tof_seconds or 0.0]], dtype=torch.float32, device=device) if model.use_tof_feature else None
    with torch.no_grad(): output = model(rir_tensor, tof_tensor)
    if task == "regression":
        coordinate = CoordinateTransform.from_dict(checkpoint["coordinate_transform"])
        position = coordinate.inverse(output.cpu())[0].numpy()
        return {"task": task, "x_m": float(position[0]), "y_m": float(position[1])}
    assert grid is not None
    probabilities, indices = topk_location_probabilities(output.cpu(), k=5)
    topk = [{"grid_index": int(index), "probability": float(probability),
             "center_xy_m": list(grid.decode(int(index)))}
            for probability, index in zip(probabilities[0], indices[0])]
    return {"task": task, "x_m": topk[0]["center_xy_m"][0], "y_m": topk[0]["center_xy_m"][1], "top_k": topk}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="从单条 RIR 推断二维 Rx 坐标")
    result.add_argument("--checkpoint", required=True, type=Path)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument("--rir", type=Path, help="rir_float32.npy")
    source.add_argument("--measurement", type=Path, help="measurements/000123 目录")
    result.add_argument("--tof-seconds", type=float)
    result.add_argument("--device", default="auto")
    return result


def main() -> int:
    args = parser().parse_args()
    measurement = args.measurement.resolve() if args.measurement else None
    rir = (measurement / "rir_float32.npy") if measurement else args.rir.resolve()
    if not rir.exists() and measurement is None:
        print(f"ERROR: RIR 不存在：{rir}", file=sys.stderr); return 2
    try:
        result = predict_single(args.checkpoint, rir, measurement_dir=measurement,
                                tof_seconds=args.tof_seconds, device_name=args.device)
    except Exception as exc:
        print(f"ERROR: inference failed: {exc}", file=sys.stderr); return 2
    print("Predicted position")
    print(f"x = {result['x_m']:.6f} m")
    print(f"y = {result['y_m']:.6f} m")
    if "top_k" in result:
        print(f"top_k = {result['top_k']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
