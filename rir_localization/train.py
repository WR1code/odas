#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rir_localization.checkpoint import load_checkpoint
from rir_localization.dataset import CoordinateTransform, GridTransform, RIRDataset
from rir_localization.evaluate import evaluate_checkpoint
from rir_localization.infer import predict_single
from rir_localization.model import build_model, parameter_count
from rir_localization.split_dataset import create_splits
from rir_localization.utils import load_config, localization_metrics, read_csv, set_seed, write_csv, write_json


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="训练 RIR 二维坐标定位 CNN")
    result.add_argument("--config", required=True, type=Path)
    result.add_argument("--smoke-test", action="store_true", help="强制 1 epoch / 最多 2 train batches 后停止")
    result.add_argument("--resume", type=Path)
    return result


def _resolve(path: str, base: Path) -> Path:
    value = Path(path).expanduser()
    return value.resolve() if value.is_absolute() else (base / value).resolve()


def _device(value: str) -> torch.device:
    if value == "auto": return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device = torch.device(value)
    if device.type == "cuda" and not torch.cuda.is_available(): raise RuntimeError("配置要求 CUDA，但 CUDA 不可用")
    return device


def _loss(name: str, task: str) -> nn.Module:
    if task == "classification": return nn.CrossEntropyLoss()
    if name == "mse": return nn.MSELoss()
    if name == "smooth_l1": return nn.SmoothL1Loss()
    raise ValueError(f"未知 regression loss：{name}")


def _epoch(model: nn.Module, loader: DataLoader[dict[str, Any]], criterion: nn.Module,
           device: torch.device, coordinate: CoordinateTransform, grid: GridTransform | None,
           optimizer: torch.optim.Optimizer | None, scaler: Any, amp: bool,
           gradient_clip: float | None, max_batches: int | None) -> tuple[float, dict[str, Any]]:
    training = optimizer is not None
    model.train(training)
    losses: list[float] = []; predictions: list[np.ndarray] = []; targets: list[np.ndarray] = []
    for batch_index, batch in enumerate(loader):
        if max_batches is not None and batch_index >= max_batches: break
        rir = batch["rir"].to(device); target = batch["target"].to(device)
        tof = batch["tof"].to(device) if getattr(model, "use_tof_feature", False) else None
        if training: optimizer.zero_grad(set_to_none=True)
        with torch.set_grad_enabled(training), torch.autocast(device_type=device.type, enabled=amp):
            output = model(rir, tof); loss = criterion(output, target)
        if training:
            scaler.scale(loss).backward()
            if gradient_clip is not None:
                scaler.unscale_(optimizer); torch.nn.utils.clip_grad_norm_(model.parameters(), gradient_clip)
            scaler.step(optimizer); scaler.update()
        losses.append(float(loss.detach().cpu()))
        if grid is None:
            pred_m = coordinate.inverse(output.detach()).cpu().numpy()
        else:
            pred_m = grid.decode_tensor(output.detach().argmax(dim=1).cpu()).numpy()
        predictions.append(pred_m); targets.append(batch["position_m"].numpy())
    if not losses: raise ValueError("DataLoader 没有产生 batch")
    pred = np.concatenate(predictions); gt = np.concatenate(targets)
    return float(np.mean(losses)), localization_metrics(pred, gt)


def train(config_path: Path, *, smoke_test: bool, resume: Path | None) -> Path:
    config_path = config_path.resolve(); config = load_config(config_path); base = config_path.parent
    experiment = config["experiment"]; data_cfg = config["data"]; split_cfg = config["split"]
    model_cfg = config["model"]; train_cfg = config["training"]; runtime_cfg = config["runtime"]
    task = str(config.get("task", "regression")); seed = int(experiment.get("seed", 42)); set_seed(seed)
    dataset_csv = _resolve(str(data_cfg["dataset_csv"]), base)
    if not dataset_csv.is_file(): raise FileNotFoundError(f"dataset_csv 不存在：{dataset_csv}")
    runs_root = _resolve(str(experiment.get("output_root", "runs")), base)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_SMOKE" if smoke_test else ""
    run_dir = runs_root / f"{stamp}_{experiment['name']}{suffix}"
    index = 1
    while run_dir.exists():
        run_dir = runs_root / f"{stamp}_{experiment['name']}{suffix}_{index:02d}"; index += 1
    checkpoint_dir = run_dir / "checkpoints"; checkpoint_dir.mkdir(parents=True)
    shutil.copy2(config_path, run_dir / "config.yaml")
    stats_path = dataset_csv.parent / "dataset_stats.json"
    if stats_path.is_file(): shutil.copy2(stats_path, run_dir / "dataset_stats.json")
    splits_dir = run_dir / "splits"
    create_splits(dataset_csv, splits_dir, mode=str(split_cfg["mode"]),
                  ratios=(float(split_cfg["train_ratio"]), float(split_cfg["val_ratio"]), float(split_cfg["test_ratio"])),
                  seed=seed, spatial_block_size_m=float(split_cfg.get("spatial_block_size_m", 0.5)))
    shutil.copy2(splits_dir / "split_spatial_map.png", run_dir / "split_spatial_map.png")
    train_rows = read_csv(splits_dir / "train.csv"); val_rows = read_csv(splits_dir / "val.csv")
    if not train_rows or not val_rows: raise ValueError("train 和 val split 都必须非空")
    coordinate = CoordinateTransform.fit(train_rows)
    grid = GridTransform.fit(train_rows, float(config.get("classification", {}).get("grid_size_m", 0.25))) if task == "classification" else None
    channels = list(map(int, data_cfg["channels"])); sample_rate = int(data_cfg["sample_rate"])
    duration_ms = float(data_cfg["rir_duration_ms"]); normalization = str(data_cfg["normalization"])
    use_tof = bool(model_cfg.get("use_tof_feature", False))
    dataset_args = dict(channels=channels, sample_rate=sample_rate, rir_duration_ms=duration_ms,
                        normalization=normalization, coordinate_transform=coordinate, task=task,
                        grid_transform=grid, use_tof_feature=use_tof)
    train_dataset = RIRDataset(splits_dir / "train.csv", **dataset_args)
    val_dataset = RIRDataset(splits_dir / "val.csv", **dataset_args)
    workers = 0 if smoke_test else int(runtime_cfg.get("num_workers", 4))
    batch_size = min(int(train_cfg["batch_size"]), len(train_dataset)) if smoke_test else int(train_cfg["batch_size"])
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=workers,
                              pin_memory=torch.cuda.is_available())
    val_loader = DataLoader(val_dataset, batch_size=int(train_cfg["batch_size"]), shuffle=False,
                            num_workers=workers, pin_memory=torch.cuda.is_available())
    device = _device(str(runtime_cfg.get("device", "auto")))
    model = build_model(model_cfg, in_channels=len(channels), task=task,
                        num_classes=None if grid is None else grid.num_classes).to(device)
    print(f"Device: {device}"); print(f"Model parameters: {parameter_count(model):,}")
    if str(train_cfg.get("optimizer", "adamw")).lower() != "adamw":
        raise ValueError("当前实现仅支持 training.optimizer=adamw")
    criterion = _loss(str(train_cfg.get("loss", "smooth_l1")), task)
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(train_cfg["learning_rate"]),
                                  weight_decay=float(train_cfg.get("weight_decay", 0.0)))
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", factor=float(train_cfg.get("scheduler_factor", 0.5)),
        patience=int(train_cfg.get("scheduler_patience", 5)))
    amp = bool(train_cfg.get("amp", True)) and device.type == "cuda"
    try:
        scaler = torch.amp.GradScaler("cuda", enabled=amp)
    except (AttributeError, TypeError):  # PyTorch 2.2 compatibility
        scaler = torch.cuda.amp.GradScaler(enabled=amp)
    start_epoch = 0; best_metric = float("inf")
    if resume:
        previous = load_checkpoint(resume, device); model.load_state_dict(previous["model_state_dict"])
        optimizer.load_state_dict(previous["optimizer_state_dict"]); scheduler.load_state_dict(previous["scheduler_state_dict"])
        start_epoch = int(previous["epoch"]) + 1; best_metric = float(previous["best_metric"])
    epochs = 1 if smoke_test else int(train_cfg["epochs"]); max_batches = 2 if smoke_test else None
    history: list[dict[str, Any]] = []; stale = 0
    split_paths = {name: str((splits_dir / f"{name}.csv").resolve()) for name in ("train", "val", "test")}
    rir_total_channels = int(train_rows[0]["rir_channels"])
    for epoch in range(start_epoch, start_epoch + epochs):
        train_loss, train_metrics = _epoch(model, train_loader, criterion, device, coordinate, grid,
                                           optimizer, scaler, amp, train_cfg.get("gradient_clip_norm"), max_batches)
        val_loss, val_metrics = _epoch(model, val_loader, criterion, device, coordinate, grid,
                                       None, scaler, amp, None, max_batches)
        metric = float(val_metrics["median_error_m"]); scheduler.step(metric)
        row = {"epoch": epoch, "train_loss": train_loss, "val_loss": val_loss,
               "train_median_error_m": train_metrics["median_error_m"],
               "val_median_error_m": metric, "learning_rate": optimizer.param_groups[0]["lr"]}
        history.append(row); print(f"epoch={epoch} train_loss={train_loss:.6g} val_median_m={metric:.6g}")
        payload = {
            "model_state_dict": model.state_dict(), "optimizer_state_dict": optimizer.state_dict(),
            "scheduler_state_dict": scheduler.state_dict(), "epoch": epoch, "best_metric": min(best_metric, metric),
            "input_channels": channels, "rir_total_channels": rir_total_channels, "sample_rate": sample_rate,
            "rir_duration_ms": duration_ms, "normalization": normalization,
            "coordinate_transform": coordinate.to_dict(), "grid_transform": None if grid is None else grid.to_dict(),
            "model_config": model_cfg, "config": config, "split_paths": split_paths,
            "smoke_test": smoke_test,
        }
        torch.save(payload, checkpoint_dir / "last.pt")
        if metric < best_metric:
            best_metric = metric; payload["best_metric"] = best_metric; torch.save(payload, checkpoint_dir / "best.pt"); stale = 0
        else: stale += 1
        if not smoke_test and stale >= int(train_cfg.get("early_stopping_patience", 15)):
            print("Early stopping"); break
    write_csv(run_dir / "history.csv", history, list(history[0]))
    fig, ax = plt.subplots(figsize=(7, 4.5)); epochs_axis = [row["epoch"] for row in history]
    ax.plot(epochs_axis, [row["train_loss"] for row in history], label="train loss")
    ax.plot(epochs_axis, [row["val_loss"] for row in history], label="val loss")
    ax.set(xlabel="Epoch", ylabel="Loss", title="Training history"); ax.grid(alpha=0.3); ax.legend(); fig.tight_layout()
    fig.savefig(run_dir / "training_curve.png", dpi=180); plt.close(fig)
    best_path = checkpoint_dir / "best.pt"
    reloaded = load_checkpoint(best_path, device)
    verify_model = build_model(model_cfg, in_channels=len(channels), task=task,
                               num_classes=None if grid is None else grid.num_classes).to(device)
    verify_model.load_state_dict(reloaded["model_state_dict"])
    if smoke_test:
        metrics = evaluate_checkpoint(best_path, split="test", output=run_dir, batch_size=batch_size,
                                      device_name=str(device))
        test_row = read_csv(splits_dir / "test.csv")[0]
        inference = predict_single(best_path, Path(test_row["rir_path"]),
                                   measurement_dir=Path(test_row["measurement_dir"]), device_name=str(device))
        # Exercise classification forward/loss/decode even when the configured baseline is regression.
        smoke_grid = GridTransform.fit(train_rows, float(config.get("classification", {}).get("grid_size_m", 0.25)))
        classifier = build_model({**model_cfg, "type": "classification_1dcnn"}, in_channels=len(channels),
                                 task="classification", num_classes=smoke_grid.num_classes).to(device)
        example = next(iter(train_loader)); logits = classifier(example["rir"].to(device),
                    example["tof"].to(device) if classifier.use_tof_feature else None)
        labels = torch.tensor([smoke_grid.encode(*point.tolist()) for point in example["position_m"]], device=device)
        classification_loss = nn.CrossEntropyLoss()(logits, labels)
        decoded = smoke_grid.decode_tensor(logits.argmax(dim=1).cpu())
        if not torch.isfinite(classification_loss) or decoded.shape[-1] != 2:
            raise RuntimeError("classification smoke test failed")
        write_json(run_dir / "smoke_test.json", {
            "status": "PASS", "epochs": epochs, "max_train_batches": max_batches,
            "checkpoint_reload": True, "evaluation_metrics": metrics, "inference": inference,
            "classification_forward_loss_decode": True,
            "warning": "SMOKE TEST metrics are not model performance results.",
        })
        print("SMOKE TEST PASS")
    return run_dir


def main() -> int:
    args = parser().parse_args()
    try:
        run_dir = train(args.config, smoke_test=args.smoke_test, resume=args.resume)
    except Exception as exc:
        print(f"ERROR: training failed: {exc}", file=sys.stderr)
        traceback.print_exc(); return 2
    print(f"Run directory: {run_dir}"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
