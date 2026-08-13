from __future__ import annotations

from pathlib import Path
from typing import Any

import torch


def load_checkpoint(path: str | Path, device: torch.device | str = "cpu") -> dict[str, Any]:
    checkpoint = torch.load(Path(path), map_location=device, weights_only=False)
    if not isinstance(checkpoint, dict) or "model_state_dict" not in checkpoint:
        raise ValueError(f"无效 checkpoint：{path}")
    return checkpoint
