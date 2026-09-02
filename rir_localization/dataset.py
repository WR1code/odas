from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import torch
from torch.utils.data import Dataset

from .utils import load_measurement_rir, read_csv


@dataclass(frozen=True)
class CoordinateTransform:
    mean: tuple[float, float]
    std: tuple[float, float]

    @classmethod
    def fit(cls, rows: Sequence[dict[str, str]]) -> "CoordinateTransform":
        if not rows:
            raise ValueError("不能从空 train split 计算 coordinate transform")
        points = np.asarray([[float(row["rx_x"]), float(row["rx_y"])] for row in rows], dtype=np.float64)
        std = np.maximum(points.std(axis=0), 1e-6)
        return cls(tuple(points.mean(axis=0).tolist()), tuple(std.tolist()))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CoordinateTransform":
        return cls(tuple(map(float, value["mean"])), tuple(map(float, value["std"])))  # type: ignore[arg-type]

    def transform(self, values: np.ndarray | torch.Tensor) -> np.ndarray | torch.Tensor:
        if isinstance(values, torch.Tensor):
            return (values - values.new_tensor(self.mean)) / values.new_tensor(self.std)
        return (values - np.asarray(self.mean)) / np.asarray(self.std)

    def inverse(self, values: np.ndarray | torch.Tensor) -> np.ndarray | torch.Tensor:
        if isinstance(values, torch.Tensor):
            return values * values.new_tensor(self.std) + values.new_tensor(self.mean)
        return values * np.asarray(self.std) + np.asarray(self.mean)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GridTransform:
    x_min: float
    y_min: float
    grid_size_m: float
    nx: int
    ny: int

    @classmethod
    def fit(cls, rows: Sequence[dict[str, str]], grid_size_m: float) -> "GridTransform":
        if not rows or grid_size_m <= 0:
            raise ValueError("GridTransform 需要非空 train split 和正 grid size")
        points = np.asarray([[float(row["rx_x"]), float(row["rx_y"])] for row in rows])
        x_min, y_min = points.min(axis=0)
        nx = max(1, int(np.floor((points[:, 0].max() - x_min) / grid_size_m)) + 1)
        ny = max(1, int(np.floor((points[:, 1].max() - y_min) / grid_size_m)) + 1)
        return cls(float(x_min), float(y_min), float(grid_size_m), nx, ny)

    @property
    def num_classes(self) -> int:
        return self.nx * self.ny

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GridTransform":
        return cls(float(value["x_min"]), float(value["y_min"]), float(value["grid_size_m"]),
                   int(value["nx"]), int(value["ny"]))

    def encode(self, x: float, y: float) -> int:
        ix = min(max(int(np.floor((x - self.x_min) / self.grid_size_m)), 0), self.nx - 1)
        iy = min(max(int(np.floor((y - self.y_min) / self.grid_size_m)), 0), self.ny - 1)
        return iy * self.nx + ix

    def decode(self, index: int) -> tuple[float, float]:
        if not 0 <= index < self.num_classes:
            raise ValueError(f"grid index {index} 超出 [0,{self.num_classes})")
        ix, iy = index % self.nx, index // self.nx
        return self.x_min + (ix + 0.5) * self.grid_size_m, self.y_min + (iy + 0.5) * self.grid_size_m

    def decode_tensor(self, indices: torch.Tensor) -> torch.Tensor:
        ix = torch.remainder(indices, self.nx).to(torch.float32)
        iy = torch.div(indices, self.nx, rounding_mode="floor").to(torch.float32)
        return torch.stack((self.x_min + (ix + 0.5) * self.grid_size_m,
                            self.y_min + (iy + 0.5) * self.grid_size_m), dim=-1)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def preprocess_rir(array: np.ndarray, *, channels: Sequence[int], target_samples: int,
                   normalization: str) -> np.ndarray:
    if array.ndim != 2:
        raise ValueError(f"内部 RIR 必须为 [C,T]，实际 {array.shape}")
    if not channels:
        raise ValueError("channels 不能为空")
    if min(channels) < 0 or max(channels) >= array.shape[0]:
        raise IndexError(f"请求 channels={list(channels)}，RIR 只有 {array.shape[0]} 通道")
    result = np.asarray(array[list(channels)], dtype=np.float32)
    if result.shape[1] >= target_samples:
        result = result[:, :target_samples]
    else:
        result = np.pad(result, ((0, 0), (0, target_samples - result.shape[1])))
    if normalization == "peak":
        denominator = float(np.max(np.abs(result)))
    elif normalization == "rms":
        denominator = float(np.sqrt(np.mean(result.astype(np.float64) ** 2)))
    elif normalization == "none":
        denominator = 1.0
    else:
        raise ValueError(f"未知 normalization：{normalization}")
    if normalization != "none" and denominator > np.finfo(np.float32).eps:
        result = result / denominator
    if not np.isfinite(result).all():
        raise ValueError("preprocessed RIR 包含 NaN/Inf")
    return np.ascontiguousarray(result, dtype=np.float32)


class RIRDataset(Dataset[dict[str, Any]]):
    def __init__(self, csv_path: str | Path, *, channels: Sequence[int], sample_rate: int,
                 rir_duration_ms: float, normalization: str = "none",
                 coordinate_transform: CoordinateTransform | None = None, task: str = "regression",
                 grid_transform: GridTransform | None = None, use_tof_feature: bool = False):
        self.csv_path = Path(csv_path)
        self.rows = read_csv(self.csv_path)
        self.channels = tuple(map(int, channels))
        self.sample_rate = int(sample_rate)
        self.target_samples = round(self.sample_rate * float(rir_duration_ms) / 1000.0)
        self.normalization = normalization
        self.coordinate_transform = coordinate_transform
        self.task = task
        self.grid_transform = grid_transform
        self.use_tof_feature = use_tof_feature
        if self.target_samples <= 0:
            raise ValueError("target RIR samples 必须为正")
        if task not in {"regression", "classification"}:
            raise ValueError(f"未知 task：{task}")
        if task == "regression" and coordinate_transform is None:
            raise ValueError("regression dataset 需要 CoordinateTransform")
        if task == "classification" and grid_transform is None:
            raise ValueError("classification dataset 需要 GridTransform")

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        row = self.rows[index]
        row_rate = int(float(row["sample_rate"]))
        if row_rate != self.sample_rate:
            raise ValueError(f"sample rate mismatch: row={row_rate}, expected={self.sample_rate}")
        array, _, _, _ = load_measurement_rir(row["measurement_dir"], int(row["rir_channels"]))
        rir = preprocess_rir(array, channels=self.channels, target_samples=self.target_samples,
                             normalization=self.normalization)
        position = np.asarray([float(row["rx_x"]), float(row["rx_y"])], dtype=np.float32)
        tof_text = row.get("tof_seconds", "")
        if self.use_tof_feature and tof_text == "":
            raise ValueError(f"use_tof_feature=true 但样本缺少 ToF：{row['measurement_dir']}")
        tof = float(tof_text) if tof_text != "" else 0.0
        item: dict[str, Any] = {
            "rir": torch.from_numpy(rir), "position_m": torch.from_numpy(position),
            "tof": torch.tensor([tof], dtype=torch.float32),
            "session_id": row["session_id"], "measurement_id": str(row["measurement_id"]),
            "rir_path": row["rir_path"],
        }
        if self.task == "regression":
            assert self.coordinate_transform is not None
            item["target"] = self.coordinate_transform.transform(item["position_m"])
        else:
            assert self.grid_transform is not None
            item["target"] = torch.tensor(self.grid_transform.encode(*position.tolist()), dtype=torch.long)
        return item
