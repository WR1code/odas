from __future__ import annotations

import csv
import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import yaml


PASS_VALUES = {"PASS", "SUCCESS", "OK", "VALID"}


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream) or {}
    if not isinstance(value, dict):
        raise ValueError(f"配置根节点必须是 mapping：{path}")
    return value


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点不是 object：{path}")
    return value


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: str | Path, rows: Sequence[dict[str, Any]], fields: Sequence[str]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def valid_position(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    numbers = [optional_float(item) for item in value[:3]]
    if any(item is None for item in numbers[:2]):
        return None
    if len(numbers) < 3 or numbers[2] is None:
        numbers = [numbers[0], numbers[1], None]
    return float(numbers[0]), float(numbers[1]), float(numbers[2] or 0.0)


def measurement_status(metadata: dict[str, Any]) -> str:
    quality = metadata.get("quality") if isinstance(metadata.get("quality"), dict) else {}
    candidates = (
        quality.get("overall"), metadata.get("status"), metadata.get("result"),
        "PASS" if quality.get("overall_pass") is True else None,
        "PASS" if metadata.get("success") is True else None,
    )
    for candidate in candidates:
        if candidate is not None:
            return str(candidate).strip().upper()
    return "UNKNOWN"


def failure_reason(metadata: dict[str, Any]) -> str:
    values = metadata.get("failure_reasons")
    if isinstance(values, list):
        result = "; ".join(str(item) for item in values if item)
        if result:
            return result
    for key in ("failure_reason", "reason", "error"):
        if metadata.get(key):
            return str(metadata[key])
    quality = metadata.get("quality", {})
    if isinstance(quality, dict) and isinstance(quality.get("quality_failure_reasons"), list):
        return "; ".join(str(item) for item in quality["quality_failure_reasons"] if item)
    return ""


def discover_sessions(root: str | Path) -> list[Path]:
    return sorted(path.parent for path in Path(root).resolve().rglob("session.json"))


def discover_measurements(root: str | Path) -> list[Path]:
    """Return only continuous-session measurement directories, excluding legacy one-shot output."""
    result: list[Path] = []
    for path in Path(root).resolve().rglob("result.json"):
        if path.parent.parent.name == "measurements" and (path.parent.parent.parent / "session.json").is_file():
            result.append(path.parent)
    return sorted(result)


def infer_rir_layout(shape: Sequence[int], expected_channels: int | None = None) -> str:
    if len(shape) != 2:
        raise ValueError(f"RIR 必须是二维数组，实际 shape={tuple(shape)}")
    first, second = int(shape[0]), int(shape[1])
    if expected_channels is not None:
        first_match, second_match = first == expected_channels, second == expected_channels
        if first_match != second_match:
            return "channels_samples" if first_match else "samples_channels"
    first_likely, second_likely = first <= 32 < second, second <= 32 < first
    if first_likely != second_likely:
        return "channels_samples" if first_likely else "samples_channels"
    raise ValueError(
        f"无法无歧义判断 RIR channel 轴：shape={tuple(shape)}, expected_channels={expected_channels}"
    )


def as_channels_samples(array: np.ndarray, expected_channels: int | None = None) -> tuple[np.ndarray, str]:
    layout = infer_rir_layout(array.shape, expected_channels)
    result = array if layout == "channels_samples" else array.T
    if result.ndim != 2:
        raise AssertionError(f"内部 RIR 不是 [C,T]：{result.shape}")
    return np.asarray(result, dtype=np.float32), layout


def load_rir(path: str | Path, expected_channels: int | None = None) -> tuple[np.ndarray, str]:
    source = Path(path)
    array = np.load(source, allow_pickle=False)
    if not isinstance(array, np.ndarray):
        raise ValueError(f"NPY 内容不是 ndarray：{source}")
    return as_channels_samples(array, expected_channels)


def load_measurement_rir(directory: str | Path, expected_channels: int | None = None) -> tuple[np.ndarray, str, Path, str]:
    """Load preferred NPY, falling back to per-channel WAV without modifying source data."""
    measurement = Path(directory)
    npy_path = measurement / "rir_float32.npy"
    if npy_path.is_file():
        array, layout = load_rir(npy_path, expected_channels)
        return array, layout, npy_path.resolve(), "npy"
    wav_paths = sorted(
        measurement.glob("rir_float32_ch*.wav"),
        key=lambda path: int(path.stem.rsplit("ch", 1)[-1]),
    )
    if not wav_paths:
        raise FileNotFoundError(f"缺少 rir_float32.npy 和逐通道 WAV：{measurement}")
    from scipy.io import wavfile
    channels: list[np.ndarray] = []
    rates: set[int] = set()
    lengths: set[int] = set()
    for path in wav_paths:
        rate, signal = wavfile.read(path)
        if signal.ndim != 1:
            raise ValueError(f"fallback WAV 不是 mono：{path} shape={signal.shape}")
        if np.issubdtype(signal.dtype, np.integer):
            scale = float(max(abs(np.iinfo(signal.dtype).min), np.iinfo(signal.dtype).max))
            signal = signal.astype(np.float32) / scale
        channels.append(np.asarray(signal, dtype=np.float32))
        rates.add(int(rate)); lengths.add(int(signal.size))
    if len(rates) != 1 or len(lengths) != 1:
        raise ValueError(f"fallback WAV 的采样率或长度不一致：{measurement}")
    if expected_channels is not None and len(channels) != expected_channels:
        raise ValueError(f"fallback WAV 通道数 {len(channels)} != expected {expected_channels}")
    return np.stack(channels), "wav_channels", measurement.resolve(), "wav_channels"


def session_channel_count(session: dict[str, Any]) -> int | None:
    device = session.get("input_device")
    if isinstance(device, dict):
        value = device.get("max_input_channels")
        if isinstance(value, int) and value > 0:
            return value
    return None


def position_from_pose(metadata: dict[str, Any], key: str) -> tuple[float, float, float] | None:
    pose = metadata.get(key)
    return valid_position(pose.get("position_m")) if isinstance(pose, dict) else None


def percentile_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    array = np.asarray(list(values), dtype=np.float64)
    array = array[np.isfinite(array)]
    if not array.size:
        return {"count": 0, "mean": None, "median": None, "std": None, "p10": None,
                "p50": None, "p75": None, "p90": None, "p95": None, "min": None, "max": None}
    return {
        "count": int(array.size), "mean": float(array.mean()), "median": float(np.median(array)),
        "std": float(array.std()), "p10": float(np.percentile(array, 10)),
        "p50": float(np.percentile(array, 50)), "p75": float(np.percentile(array, 75)),
        "p90": float(np.percentile(array, 90)), "p95": float(np.percentile(array, 95)),
        "min": float(array.min()), "max": float(array.max()),
    }


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    try:
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
    except ImportError:
        pass


def status_distribution(measurements: Iterable[Path]) -> Counter[str]:
    result: Counter[str] = Counter()
    for directory in measurements:
        try:
            result[measurement_status(read_json(directory / "result.json"))] += 1
        except (OSError, ValueError, json.JSONDecodeError):
            result["BAD_JSON"] += 1
    return result


def localization_metrics(predictions: np.ndarray, targets: np.ndarray) -> dict[str, float | int]:
    if predictions.shape != targets.shape or predictions.ndim != 2 or predictions.shape[1] != 2:
        raise ValueError(f"metrics 需要相同 [N,2] 数组：pred={predictions.shape}, gt={targets.shape}")
    errors = np.linalg.norm(predictions.astype(np.float64) - targets.astype(np.float64), axis=1)
    if not errors.size:
        raise ValueError("不能评估空 prediction")
    return {
        "samples": int(errors.size), "mean_error_m": float(errors.mean()),
        "median_error_m": float(np.median(errors)), "p50_error_m": float(np.percentile(errors, 50)),
        "p75_error_m": float(np.percentile(errors, 75)), "p90_error_m": float(np.percentile(errors, 90)),
        "p95_error_m": float(np.percentile(errors, 95)),
        "rmse_m": float(np.sqrt(np.mean(errors ** 2))), "min_error_m": float(errors.min()),
        "max_error_m": float(errors.max()),
    }
