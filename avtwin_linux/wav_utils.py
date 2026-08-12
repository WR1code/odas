from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

import numpy as np
from scipy import signal
from scipy.io import wavfile

from .config import SAMPLE_RATE


def wav_metadata(path: Path) -> dict[str, int | float | str]:
    rate, data = wavfile.read(path, mmap=True)
    channels = 1 if data.ndim == 1 else int(data.shape[1])
    return {
        "path": str(path.resolve()),
        "sample_rate": int(rate),
        "channels": channels,
        "frames": int(data.shape[0]),
        "duration_s": float(data.shape[0] / rate),
        "dtype": str(data.dtype),
    }


def _to_float32(data: np.ndarray) -> np.ndarray:
    if np.issubdtype(data.dtype, np.floating):
        return np.asarray(data, dtype=np.float32)
    if data.dtype == np.uint8:
        return (data.astype(np.float32) - 128.0) / 128.0
    if np.issubdtype(data.dtype, np.signedinteger):
        scale = float(max(abs(np.iinfo(data.dtype).min), np.iinfo(data.dtype).max))
        return data.astype(np.float32) / scale
    raise ValueError(f"不支持的 WAV 数据类型：{data.dtype}")


def load_probe(
    path: Path, *, warning: Callable[[str], None] = print
) -> tuple[np.ndarray, list[str]]:
    rate, raw = wavfile.read(path)
    warnings: list[str] = []
    original_dtype = raw.dtype
    if raw.ndim == 2:
        channels = _to_float32(raw)
        rms = np.sqrt(np.mean(np.asarray(channels, dtype=np.float64) ** 2, axis=0))
        strongest = int(np.argmax(rms))
        others = np.delete(rms, strongest)
        if not others.size or float(rms[strongest]) >= 10.0 * max(float(np.max(others)), 1e-12):
            warnings.append(
                f"{path.name} 不是 mono；CH{strongest} 是唯一/主导有效声道，"
                "已选择该声道（未做会降低幅度的通道平均）"
            )
            raw = channels[:, strongest]
        else:
            warnings.append(f"{path.name} 不是 mono，已将 {raw.shape[1]} 通道平均为 mono")
            raw = channels.mean(axis=1)
    else:
        raw = _to_float32(raw)
    if rate != SAMPLE_RATE:
        warnings.append(f"{path.name} 采样率为 {rate} Hz，已重采样至 {SAMPLE_RATE} Hz")
        divisor = math.gcd(int(rate), SAMPLE_RATE)
        raw = signal.resample_poly(raw, SAMPLE_RATE // divisor, int(rate) // divisor)
    if original_dtype != np.int16:
        warnings.append(f"{path.name} 编码为 {original_dtype}，正式测量建议使用 PCM16")
    probe = np.ascontiguousarray(raw, dtype=np.float32)
    if probe.size < 16 or not np.all(np.isfinite(probe)):
        raise ValueError(f"{path} 不是有效的 chirp 模板")
    probe -= float(np.mean(probe))
    if float(np.max(np.abs(probe))) < 1e-8:
        raise ValueError(f"{path} 波形为静音")
    for message in warnings:
        warning("WARNING: " + message)
    return probe, warnings


def write_pcm16(path: Path, data: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(np.asarray(data, dtype=np.float64), -1.0, 1.0 - 1.0 / 32768.0)
    wavfile.write(path, sample_rate, np.rint(clipped * 32768.0).astype(np.int16))


def write_float32(path: Path, data: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    """Write IEEE Float32 WAV without normalization or per-channel scaling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    values = np.ascontiguousarray(data, dtype=np.float32)
    if not np.all(np.isfinite(values)):
        raise ValueError(f"{path} 包含非有限 Float32 样本")
    wavfile.write(path, sample_rate, values)
