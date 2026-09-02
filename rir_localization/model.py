from __future__ import annotations

from typing import Any

import torch
from torch import nn


class ConvBlock(nn.Sequential):
    def __init__(self, input_channels: int, output_channels: int, kernel_size: int, stride: int):
        super().__init__(
            nn.Conv1d(input_channels, output_channels, kernel_size, stride=stride,
                      padding=kernel_size // 2, bias=False),
            nn.BatchNorm1d(output_channels), nn.ReLU(inplace=True),
        )


class RIR1DCNN(nn.Module):
    """Length-agnostic lightweight CNN for regression or location-cell classification."""
    def __init__(self, *, in_channels: int, base_channels: int = 32, dropout: float = 0.2,
                 task: str = "regression", num_classes: int | None = None,
                 use_tof_feature: bool = False):
        super().__init__()
        if in_channels < 1 or base_channels < 4:
            raise ValueError("in_channels 必须 >=1，base_channels 必须 >=4")
        if task not in {"regression", "classification"}:
            raise ValueError(f"未知 task：{task}")
        if task == "classification" and (num_classes is None or num_classes < 1):
            raise ValueError("classification 需要正 num_classes")
        widths = [base_channels * factor for factor in (1, 2, 4, 8)]
        self.encoder = nn.Sequential(
            ConvBlock(in_channels, widths[0], 31, 4),
            ConvBlock(widths[0], widths[1], 15, 4),
            ConvBlock(widths[1], widths[2], 9, 2),
            ConvBlock(widths[2], widths[3], 5, 2),
            nn.AdaptiveAvgPool1d(1), nn.Flatten(),
        )
        self.task = task
        self.use_tof_feature = use_tof_feature
        embedding_size = widths[-1]
        if use_tof_feature:
            self.tof_encoder = nn.Sequential(nn.Linear(1, 16), nn.ReLU(inplace=True))
            embedding_size += 16
        else:
            self.tof_encoder = None
        outputs = 2 if task == "regression" else int(num_classes)
        self.head = nn.Sequential(
            nn.Linear(embedding_size, widths[2]), nn.ReLU(inplace=True), nn.Dropout(dropout),
            nn.Linear(widths[2], outputs),
        )

    def forward(self, rir: torch.Tensor, tof: torch.Tensor | None = None) -> torch.Tensor:
        if rir.ndim != 3:
            raise ValueError(f"CNN input 必须是 [B,C,T]，实际 {tuple(rir.shape)}")
        embedding = self.encoder(rir)
        if self.use_tof_feature:
            if tof is None:
                raise ValueError("use_tof_feature=true 时 forward 必须提供 tof")
            if tof.ndim == 1:
                tof = tof[:, None]
            assert self.tof_encoder is not None
            embedding = torch.cat((embedding, self.tof_encoder(tof)), dim=1)
        return self.head(embedding)


def build_model(model_config: dict[str, Any], *, in_channels: int, task: str,
                num_classes: int | None = None) -> RIR1DCNN:
    model_type = model_config.get("type", "regression_1dcnn")
    if model_type not in {"regression_1dcnn", "classification_1dcnn"}:
        raise ValueError(f"不支持 model.type={model_type}")
    return RIR1DCNN(
        in_channels=in_channels, base_channels=int(model_config.get("base_channels", 32)),
        dropout=float(model_config.get("dropout", 0.2)), task=task, num_classes=num_classes,
        use_tof_feature=bool(model_config.get("use_tof_feature", False)),
    )


def parameter_count(model: nn.Module) -> int:
    return sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)


def topk_location_probabilities(logits: torch.Tensor, k: int = 5) -> tuple[torch.Tensor, torch.Tensor]:
    probabilities = torch.softmax(logits, dim=-1)
    return torch.topk(probabilities, k=min(k, probabilities.shape[-1]), dim=-1)
