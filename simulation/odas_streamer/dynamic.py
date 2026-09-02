"""Pose-triggered RIR cache and click-free block convolution primitives."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import time

import numpy as np
from scipy.signal import fftconvolve


class FIRBlock:
    def __init__(self, rirs: np.ndarray):
        self.rirs = np.asarray(rirs, dtype=np.float64)
        if self.rirs.ndim != 2:
            raise ValueError("RIR must be [channels,taps]")
        self.history = np.zeros(max(0, self.rirs.shape[1] - 1), dtype=np.float64)

    def process(self, mono: np.ndarray) -> np.ndarray:
        mono = np.asarray(mono, dtype=np.float64)
        joined = np.concatenate((self.history, mono))
        offset = len(self.history)
        out = np.stack([
            fftconvolve(joined, rir, mode="full")[offset:offset + len(mono)]
            for rir in self.rirs
        ], axis=1)
        if len(self.history):
            self.history = joined[-len(self.history):]
        return out


class CrossfadingConvolver:
    def __init__(self, rirs: np.ndarray, fade_samples: int = 2400):
        self.current = FIRBlock(rirs)
        self.pending: FIRBlock | None = None
        self.fade_samples = max(1, int(fade_samples))
        self.fade_position = 0
        self.version = 1

    def update(self, rirs: np.ndarray) -> None:
        self.pending = FIRBlock(rirs)
        self.pending.history = self.current.history.copy()
        self.fade_position = 0
        self.version += 1

    def process(self, mono: np.ndarray) -> np.ndarray:
        old = self.current.process(mono)
        if self.pending is None:
            return old
        new = self.pending.process(mono)
        alpha = np.clip((self.fade_position + np.arange(len(mono))) / self.fade_samples, 0.0, 1.0)[:, None]
        result = old * (1.0 - alpha) + new * alpha
        self.fade_position += len(mono)
        if self.fade_position >= self.fade_samples:
            self.current = self.pending
            self.pending = None
        return result


@dataclass
class RIRUpdatePolicy:
    position_threshold_m: float = 0.05
    angle_threshold_deg: float = 3.0
    max_update_hz: float = 2.0
    cache_size: int = 128
    quantization_m: float = 0.02


class RIRCache:
    def __init__(self, policy: RIRUpdatePolicy):
        self.policy = policy
        self.entries: OrderedDict[tuple[int, ...], np.ndarray] = OrderedDict()
        self.last_update_monotonic = -float("inf")
        self.last_source_m: np.ndarray | None = None
        self.last_microphones_m: np.ndarray | None = None

    def key(self, source_m: np.ndarray, microphones_m: np.ndarray) -> tuple[int, ...]:
        values = np.concatenate((np.asarray(source_m).ravel(), np.asarray(microphones_m).ravel()))
        return tuple(np.rint(values / self.policy.quantization_m).astype(int).tolist())

    def get(self, source_m: np.ndarray, microphones_m: np.ndarray) -> np.ndarray | None:
        key = self.key(source_m, microphones_m)
        value = self.entries.get(key)
        if value is not None:
            self.entries.move_to_end(key)
        return value

    def put(self, source_m: np.ndarray, microphones_m: np.ndarray, rirs: np.ndarray) -> None:
        key = self.key(source_m, microphones_m)
        self.entries[key] = np.asarray(rirs)
        self.entries.move_to_end(key)
        while len(self.entries) > self.policy.cache_size:
            self.entries.popitem(last=False)
        self.last_update_monotonic = time.monotonic()
        self.last_source_m = np.asarray(source_m, dtype=float).copy()
        self.last_microphones_m = np.asarray(microphones_m, dtype=float).copy()

    def update_allowed(self) -> bool:
        return time.monotonic() - self.last_update_monotonic >= 1.0 / self.policy.max_update_hz

    def pose_changed(self, source_m: np.ndarray, microphones_m: np.ndarray) -> bool:
        if self.last_source_m is None or self.last_microphones_m is None:
            return True
        source_delta = np.linalg.norm(np.asarray(source_m) - self.last_source_m)
        microphones = np.asarray(microphones_m)
        microphone_delta = np.max(np.linalg.norm(microphones - self.last_microphones_m, axis=1))
        angle_delta_deg = 0.0
        if len(microphones) > 1:
            current_axis = microphones[1] - microphones[0]
            previous_axis = self.last_microphones_m[1] - self.last_microphones_m[0]
            denominator = np.linalg.norm(current_axis) * np.linalg.norm(previous_axis)
            if denominator:
                cosine = np.clip(np.dot(current_axis, previous_axis) / denominator, -1.0, 1.0)
                angle_delta_deg = float(np.degrees(np.arccos(cosine)))
        return (max(source_delta, microphone_delta) >= self.policy.position_threshold_m
                or angle_delta_deg >= self.policy.angle_threshold_deg)


class DynamicAudioEngine:
    """Bind pose thresholds, cache, AcoustiX callback, convolution, and block metadata."""
    def __init__(self, initial_rirs: np.ndarray, policy: RIRUpdatePolicy | None = None,
                 fade_samples: int = 2400):
        self.cache = RIRCache(policy or RIRUpdatePolicy())
        self.convolver = CrossfadingConvolver(initial_rirs, fade_samples)
        self.rir_updates = 0

    def process(self, mono_block: np.ndarray, *, sim_time_s: float, source_m: np.ndarray,
                microphones_m: np.ndarray, compute_rirs) -> tuple[np.ndarray, dict]:
        cached = self.cache.get(source_m, microphones_m)
        cache_hit = cached is not None
        update_seconds = 0.0
        if cached is not None and self.cache.pose_changed(source_m, microphones_m):
            self.convolver.update(cached)
        elif cached is None and self.cache.pose_changed(source_m, microphones_m) and self.cache.update_allowed():
            started = time.perf_counter()
            rirs = compute_rirs(np.asarray(source_m), np.asarray(microphones_m))
            update_seconds = time.perf_counter() - started
            self.cache.put(source_m, microphones_m, rirs)
            self.convolver.update(rirs)
            self.rir_updates += 1
        output = self.convolver.process(mono_block)
        metadata = {"sim_time_s": float(sim_time_s), "source_position_m": np.asarray(source_m).tolist(),
                    "microphone_positions_m": np.asarray(microphones_m).tolist(),
                    "rir_version": self.convolver.version, "rir_cache_hit": cache_hit,
                    "rir_update_seconds": update_seconds}
        return output, metadata
