"""WAV, convolution, and ODAS S32_LE interleaved PCM utilities."""
from __future__ import annotations

import math
from pathlib import Path
import wave

import numpy as np
from scipy.signal import fftconvolve, resample_poly


INT32_PEAK = np.iinfo(np.int32).max


def read_mono_wav(path: str | Path, target_rate: int = 48000) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        channels, width, rate, count = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
        raw = wav.readframes(count)
    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"only 16/32-bit PCM WAV is supported, got {width * 8} bits")
    samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != target_rate:
        divisor = math.gcd(rate, target_rate)
        samples = resample_poly(samples, target_rate // divisor, rate // divisor)
    return np.asarray(samples, dtype=np.float64)


def write_pcm_wav(path: str | Path, audio: np.ndarray, sample_rate: int = 48000) -> None:
    data = np.asarray(audio)
    if data.ndim == 1:
        data = data[:, None]
    pcm = float_to_s32le(data)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(data.shape[1])
        wav.setsampwidth(4)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes(order="C"))


def convolve_mono_rirs(clean: np.ndarray, rirs: np.ndarray) -> np.ndarray:
    rirs = np.asarray(rirs, dtype=np.float64)
    if rirs.ndim != 2:
        raise ValueError("rirs must have shape [channels, samples]")
    return np.stack([fftconvolve(clean, rir, mode="full") for rir in rirs], axis=1)


def normalize_peak(audio: np.ndarray, peak: float = 0.8) -> tuple[np.ndarray, float]:
    maximum = float(np.max(np.abs(audio))) if audio.size else 0.0
    scale = 1.0 if maximum == 0 else min(1.0, peak / maximum)
    return np.asarray(audio, dtype=np.float64) * scale, scale


def with_zero_hardware_channel(active_audio: np.ndarray) -> np.ndarray:
    """Return eight hardware channels; channel 8 is silence and mapping 1..7 ignores it."""
    active = np.asarray(active_audio, dtype=np.float64)
    if active.ndim != 2 or active.shape[1] != 7:
        raise ValueError("UMA-8 active audio must contain exactly seven channels")
    return np.pad(active, ((0, 0), (0, 1)), mode="constant")


def float_to_s32le(audio: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(audio, dtype=np.float64), -1.0, 1.0)
    return np.rint(clipped * INT32_PEAK).astype("<i4")


def write_raw_s32le(path: str | Path, audio: np.ndarray) -> dict[str, int | float]:
    pcm = float_to_s32le(audio)
    Path(path).write_bytes(pcm.tobytes(order="C"))
    return {
        "frames": int(pcm.shape[0]),
        "channels": int(pcm.shape[1]),
        "clipped_samples": int(np.count_nonzero(np.abs(audio) > 1.0)),
        "bytes": int(pcm.nbytes),
    }


def read_raw_s32le(path: str | Path, channels: int = 8) -> np.ndarray:
    data = np.fromfile(path, dtype="<i4")
    if data.size % channels:
        raise ValueError("RAW byte length is not an integer number of frames")
    return data.reshape(-1, channels)
