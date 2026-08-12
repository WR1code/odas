from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .config import SAMPLE_RATE
from .matched_filter import bandlimit_for_template, normalized_correlation


def _save(fig: Any, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_overview(path: Path, recording: np.ndarray, result: dict[str, Any]) -> None:
    fig, axis = plt.subplots(figsize=(13, 6))
    if recording.size:
        step = max(1, recording.shape[0] // 12000)
        time_s = np.arange(0, recording.shape[0], step) / SAMPLE_RATE
        scale = max(float(np.max(np.abs(recording))), 1e-6)
        for ch in range(recording.shape[1]):
            axis.plot(time_s, recording[::step, ch] / scale + ch * 1.4, lw=0.45, label=f"CH{ch}")
    for key, color in (("c1_detection", "#d62728"), ("c2_detection", "#2ca02c")):
        sample = result[key]["system_sample"]
        if sample is not None:
            axis.axvline(sample / SAMPLE_RATE, color=color, lw=1.5, label=key[:2].upper())
    axis.set(title="Raw Linux 8-channel continuous recording", xlabel="Time (s)", ylabel="Channels (offset)")
    axis.grid(alpha=0.2)
    axis.legend(ncol=5, fontsize=8)
    _save(fig, path)


def plot_detection(
    path: Path,
    recording: np.ndarray,
    template: np.ndarray,
    detection: dict[str, Any],
    title: str,
) -> None:
    fig, axis = plt.subplots(figsize=(12, 5))
    center = detection["system_sample"]
    if center is None:
        start, stop = 0, recording.shape[0]
    else:
        margin = round(0.15 * SAMPLE_RATE)
        start, stop = max(0, center - margin), min(recording.shape[0], center + template.size + margin)
    filtered_recording, filtered_template, passband = bandlimit_for_template(
        recording[start:stop], template
    )
    for ch in range(recording.shape[1]):
        if stop - start < template.size:
            continue
        score = normalized_correlation(filtered_recording[:, ch], filtered_template)
        axis.plot((np.arange(score.size) + start) / SAMPLE_RATE, score, lw=0.7, label=f"CH{ch}")
    if center is not None:
        axis.axvline(center / SAMPLE_RATE, color="black", linestyle="--", label="fused arrival")
    band_text = "full band" if passband is None else f"template band {passband[0]:.0f}–{passband[1]:.0f} Hz"
    axis.set(title=f"{title} — {band_text}", xlabel="Template start on Linux PCM timeline (s)", ylabel="Normalized score", ylim=(0, 1.02))
    axis.grid(alpha=0.25)
    axis.legend(ncol=5, fontsize=8)
    _save(fig, path)


def plot_rirs(
    all_path: Path, early_path: Path, rirs: np.ndarray, direct_arrival_index: int = 0,
) -> None:
    time_ms = (np.arange(rirs.shape[0]) - direct_arrival_index) * 1000.0 / SAMPLE_RATE
    for path, limit, title in (
        (all_path, time_ms[-1] if time_ms.size else 0, "RIR - all channels"),
        (early_path, 50.0, "RIR - pre-arrival and first 50 ms"),
    ):
        fig, axis = plt.subplots(figsize=(12, 5))
        for ch in range(rirs.shape[1]):
            axis.plot(time_ms, rirs[:, ch], lw=0.7, label=f"CH{ch}")
        left = float(time_ms[0]) if time_ms.size else 0.0
        axis.set(title=title, xlabel="Time relative to C2 arrival (ms)", ylabel="RIR amplitude", xlim=(left, max(limit, 1)))
        axis.grid(alpha=0.25)
        axis.legend(ncol=4, fontsize=8)
        _save(fig, path)


def write_all_plots(
    directory: Path,
    recording: np.ndarray,
    c1: np.ndarray,
    c2: np.ndarray,
    result: dict[str, Any],
    rirs: np.ndarray,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    plot_overview(directory / "raw_overview.png", recording, result)
    plot_detection(directory / "c1_detection.png", recording, c1, result["c1_detection"], "C1 normalized matched-filter detection")
    plot_detection(directory / "c2_detection.png", recording, c2, result["c2_detection"], "C2 normalized matched-filter detection")
    plot_rirs(
        directory / "rir_all_channels.png", directory / "rir_first_50ms.png", rirs,
        int(result.get("rir", {}).get("direct_arrival_index", 0)),
    )
