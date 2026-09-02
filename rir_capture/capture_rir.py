#!/usr/bin/env python3
"""Capture a real-room, 8-channel UMA-8 matched-filter RIR estimate.

This first engineering implementation uses a linear chirp. It intentionally
does not perform AGC, AEC, noise suppression, beamforming, per-channel
normalization, ToF correction, or simulation.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy import signal
from scipy.io import wavfile
import sounddevice as sd


SAMPLE_RATE = 48_000
INPUT_CHANNELS = 8
CHIRP_START_HZ = 50.0
CHIRP_END_HZ = 9_000.0
CHIRP_DURATION_S = 0.2
CHIRP_AMPLITUDE = 0.4
PRE_SILENCE_S = 0.5
POST_SILENCE_S = 1.0
RECORDING_DURATION_S = 2.0
RIR_DURATION_S = 0.5
DEFAULT_INPUT_MATCH = "micArray RAW SPK"
DEFAULT_OUTPUT_MATCH = "HECATE G2无线版 HEADSET"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用真实扬声器和 UMA-8 采集一次 8 通道 RIR"
    )
    parser.add_argument("--input-device", help="PortAudio 输入 index 或设备名")
    parser.add_argument("--output-device", help="PortAudio 输出 index 或设备名")
    parser.add_argument("--output-channel", type=int, default=0)
    parser.add_argument("--amplitude", type=float, default=CHIRP_AMPLITUDE)
    parser.add_argument("--countdown", type=int, default=3)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parent / "output",
    )
    parser.add_argument("--list-devices", action="store_true")
    return parser


def resolve_device(value: str | None, *, input_device: bool) -> int:
    devices = sd.query_devices()
    channel_key = "max_input_channels" if input_device else "max_output_channels"
    required_channels = INPUT_CHANNELS if input_device else 1

    if value is not None:
        try:
            index = int(value)
        except ValueError:
            matches = [
                i for i, device in enumerate(devices)
                if value.casefold() in str(device["name"]).casefold()
                and int(device[channel_key]) >= required_channels
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"设备名 {value!r} 匹配到 {matches}；请传入明确的 PortAudio index"
                )
            index = matches[0]
        device = sd.query_devices(index)
        if int(device[channel_key]) < required_channels:
            raise ValueError(
                f"设备 {index} 只有 {device[channel_key]} 个所需方向的通道"
            )
        return index

    default_match = DEFAULT_INPUT_MATCH if input_device else DEFAULT_OUTPUT_MATCH
    matches = [
        i for i, device in enumerate(devices)
        if default_match.casefold() in str(device["name"]).casefold()
        and int(device[channel_key]) >= required_channels
    ]
    if len(matches) != 1:
        direction = "输入" if input_device else "输出"
        raise ValueError(
            f"无法唯一自动选择 {default_match!r} {direction}设备，候选为 {matches}；"
            f"请使用 --{'input' if input_device else 'output'}-device"
        )
    return matches[0]


def device_metadata(index: int) -> dict[str, Any]:
    device = sd.query_devices(index)
    hostapi = sd.query_hostapis(int(device["hostapi"]))
    return {
        "index": index,
        "name": str(device["name"]),
        "hostapi": str(hostapi["name"]),
        "max_input_channels": int(device["max_input_channels"]),
        "max_output_channels": int(device["max_output_channels"]),
        "default_samplerate": float(device["default_samplerate"]),
    }


def make_chirp() -> np.ndarray:
    count = round(CHIRP_DURATION_S * SAMPLE_RATE)
    t = np.arange(count, dtype=np.float64) / SAMPLE_RATE
    # Initial engineering implementation uses a linear chirp because the
    # referenced paper parameters do not specify linear versus logarithmic.
    chirp = signal.chirp(
        t, f0=CHIRP_START_HZ, f1=CHIRP_END_HZ,
        t1=CHIRP_DURATION_S, method="linear", phi=-90.0,
    )
    chirp *= signal.windows.tukey(count, alpha=0.1)
    return np.asarray(CHIRP_AMPLITUDE * chirp, dtype=np.float32)


def make_playback(chirp: np.ndarray) -> np.ndarray:
    pre = np.zeros(round(PRE_SILENCE_S * SAMPLE_RATE), dtype=np.float32)
    post = np.zeros(round(POST_SILENCE_S * SAMPLE_RATE), dtype=np.float32)
    return np.concatenate((pre, chirp, post))


def write_float_wav(path: Path, data: np.ndarray) -> None:
    wavfile.write(path, SAMPLE_RATE, np.asarray(data, dtype=np.float32))


def capture_duplex(
    playback: np.ndarray,
    *,
    input_device: int,
    output_device: int,
    output_channels: int,
    output_channel: int,
) -> tuple[np.ndarray, list[str]]:
    total_frames = round(RECORDING_DURATION_S * SAMPLE_RATE)
    recording = np.zeros((total_frames, INPUT_CHANNELS), dtype=np.float32)
    padded_playback = np.zeros(total_frames, dtype=np.float32)
    padded_playback[: min(total_frames, playback.size)] = playback[:total_frames]
    finished = threading.Event()
    status_messages: list[str] = []
    cursor = 0

    def callback(
        indata: np.ndarray,
        outdata: np.ndarray,
        frames: int,
        _time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        nonlocal cursor
        if status:
            status_messages.append(str(status))
        outdata.fill(0.0)
        remaining = total_frames - cursor
        count = min(frames, remaining)
        if count > 0:
            recording[cursor : cursor + count] = indata[:count]
            outdata[:count, output_channel] = padded_playback[cursor : cursor + count]
            cursor += count
        if cursor >= total_frames:
            raise sd.CallbackStop

    with sd.Stream(
        device=(input_device, output_device),
        samplerate=SAMPLE_RATE,
        channels=(INPUT_CHANNELS, output_channels),
        dtype=("float32", "float32"),
        callback=callback,
        finished_callback=finished.set,
        blocksize=0,
        latency="high",
    ):
        if not finished.wait(RECORDING_DURATION_S + 5.0):
            raise TimeoutError("音频流没有在预期时间内结束")

    if cursor != total_frames:
        raise RuntimeError(f"录音长度异常：期望 {total_frames}，实际 {cursor}")
    return recording, status_messages


def sustained_onset(mask: np.ndarray, required: int) -> int | None:
    if mask.size < required:
        return None
    runs = np.convolve(mask.astype(np.int16), np.ones(required, dtype=np.int16), mode="valid")
    found = np.flatnonzero(runs >= required)
    return int(found[0]) if found.size else None


def extract_rirs(
    recording: np.ndarray,
    chirp: np.ndarray,
) -> tuple[np.ndarray, list[dict[str, Any]], dict[str, Any]]:
    chirp64 = chirp.astype(np.float64)
    chirp_energy = float(np.dot(chirp64, chirp64))
    lags = signal.correlation_lags(recording.shape[0], chirp.size, mode="full")
    search_min_lag = round((PRE_SILENCE_S - 0.05) * SAMPLE_RATE)
    search_max_lag = round((PRE_SILENCE_S + 0.60) * SAMPLE_RATE)
    search_indices = np.flatnonzero((lags >= search_min_lag) & (lags <= search_max_lag))
    noise_indices = np.flatnonzero(
        (lags >= 0) & (lags <= round((PRE_SILENCE_S - 0.10) * SAMPLE_RATE))
    )
    if not search_indices.size or not noise_indices.size:
        raise RuntimeError("相关搜索窗口无效")

    correlations: list[np.ndarray] = []
    channel_info: list[dict[str, Any]] = []
    onset_lags: list[int] = []
    envelope_window = max(3, round(0.00025 * SAMPLE_RATE))
    required_run = max(2, round(0.00010 * SAMPLE_RATE))

    for channel in range(recording.shape[1]):
        corr = signal.correlate(
            recording[:, channel].astype(np.float64), chirp64,
            mode="full", method="fft",
        ) / chirp_energy
        correlations.append(corr)
        power_envelope = np.sqrt(
            np.convolve(corr * corr, np.ones(envelope_window) / envelope_window, mode="same")
        )
        search_envelope = power_envelope[search_indices]
        peak_local = int(np.argmax(np.abs(corr[search_indices])))
        peak_index = int(search_indices[peak_local])
        peak_lag = int(lags[peak_index])

        noise_values = power_envelope[noise_indices]
        noise_correlation = np.abs(corr[noise_indices])
        noise_median = float(np.median(noise_values))
        noise_mad = float(np.median(np.abs(noise_values - noise_median)))
        robust_sigma = 1.4826 * noise_mad
        threshold = max(
            noise_median + 10.0 * robust_sigma,
            0.05 * float(search_envelope.max()),
            np.finfo(np.float64).eps,
        )
        before_peak = search_indices[search_indices <= peak_index]
        onset_local = sustained_onset(power_envelope[before_peak] >= threshold, required_run)
        fallback = onset_local is None
        if fallback:
            onset_lag = peak_lag - round(0.002 * SAMPLE_RATE)
        else:
            onset_lag = int(lags[int(before_peak[onset_local])])
        onset_lags.append(onset_lag)
        channel_info.append({
            "channel": channel,
            "correlation_peak": float(corr[peak_index]),
            "correlation_peak_abs": float(abs(corr[peak_index])),
            "correlation_peak_lag_samples": peak_lag,
            "correlation_peak_lag_ms": 1000.0 * peak_lag / SAMPLE_RATE,
            "correlation_noise_median_abs": float(np.median(noise_correlation)),
            "correlation_peak_to_noise_median_ratio": float(
                abs(corr[peak_index]) /
                max(float(np.median(noise_correlation)), np.finfo(np.float64).eps)
            ),
            "onset_candidate_lag_samples": onset_lag,
            "onset_candidate_lag_ms": 1000.0 * onset_lag / SAMPLE_RATE,
            "onset_threshold": threshold,
            "onset_fallback_from_peak": fallback,
        })

    onset_channel_indices = [
        item["channel"] for item in channel_info
        if item["correlation_peak_to_noise_median_ratio"] >= 10.0
        and item["correlation_peak_abs"] > 1e-10
    ]
    if not onset_channel_indices:
        onset_channel_indices = list(range(recording.shape[1]))
    onset_array = np.asarray(
        [onset_lags[channel] for channel in onset_channel_indices], dtype=np.int64
    )
    median_onset = int(np.median(onset_array))
    onset_spread = int(onset_array.max() - onset_array.min())
    if onset_spread <= round(0.005 * SAMPLE_RATE):
        global_start_lag = max(0, int(onset_array.min()) - round(0.002 * SAMPLE_RATE))
        onset_policy = "earliest_significant_channel_onset_minus_2ms"
    else:
        global_start_lag = median_onset - round(0.005 * SAMPLE_RATE)
        onset_policy = "median_onset_minus_5ms_due_to_outlier_spread"

    rir_samples = round(RIR_DURATION_S * SAMPLE_RATE)
    rirs = np.zeros((rir_samples, recording.shape[1]), dtype=np.float64)
    lag_to_index = chirp.size - 1
    start_index = global_start_lag + lag_to_index
    for channel, corr in enumerate(correlations):
        source_start = max(0, start_index)
        source_end = min(corr.size, start_index + rir_samples)
        destination_start = source_start - start_index
        destination_end = destination_start + (source_end - source_start)
        rirs[destination_start:destination_end, channel] = corr[source_start:source_end]

    common_info = {
        "global_rir_start_lag_samples": global_start_lag,
        "global_rir_start_lag_ms": 1000.0 * global_start_lag / SAMPLE_RATE,
        "onset_candidate_spread_samples": onset_spread,
        "onset_candidate_spread_ms": 1000.0 * onset_spread / SAMPLE_RATE,
        "onset_channels_used": onset_channel_indices,
        "onset_channels_excluded": [
            channel for channel in range(recording.shape[1])
            if channel not in onset_channel_indices
        ],
        "onset_policy": onset_policy,
        "rir_duration_s": RIR_DURATION_S,
        "matched_filter_scaling": "correlation divided by chirp energy; no per-channel normalization",
        "peak_interpretation": "candidate only; not asserted to be the direct path",
    }
    return rirs, channel_info, common_info


def plot_rirs(output: Path, rirs: np.ndarray) -> None:
    time_ms = np.arange(rirs.shape[0]) * 1000.0 / SAMPLE_RATE
    for channel in range(rirs.shape[1]):
        fig, axis = plt.subplots(figsize=(10, 4))
        axis.plot(time_ms, rirs[:, channel], linewidth=0.7)
        axis.set(xlabel="Time (ms)", ylabel="Matched-filter amplitude",
                 title=f"Real RIR estimate - channel {channel}", xlim=(0, 500))
        axis.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(output / f"rir_ch{channel}.png", dpi=150)
        plt.close(fig)

    fig, axis = plt.subplots(figsize=(12, 6))
    for channel in range(rirs.shape[1]):
        axis.plot(time_ms, rirs[:, channel], linewidth=0.7, label=f"CH{channel}")
    axis.set(xlabel="Time (ms)", ylabel="Matched-filter amplitude",
             title="Real RIR estimates - all UMA-8 channels", xlim=(0, 500))
    axis.grid(True, alpha=0.25)
    axis.legend(ncol=4)
    fig.tight_layout()
    fig.savefig(output / "rir_all_channels.png", dpi=170)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(12, 6))
    early_samples = round(0.05 * SAMPLE_RATE)
    for channel in range(rirs.shape[1]):
        axis.plot(time_ms[:early_samples], rirs[:early_samples, channel],
                  linewidth=0.8, label=f"CH{channel}")
    axis.set(xlabel="Time (ms)", ylabel="Matched-filter amplitude",
             title="Real RIR estimates - first 50 ms", xlim=(0, 50))
    axis.grid(True, alpha=0.25)
    axis.legend(ncol=4)
    fig.tight_layout()
    fig.savefig(output / "rir_early_50ms.png", dpi=170)
    plt.close(fig)


def main() -> int:
    args = build_parser().parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return 0
    if not 0.0 < args.amplitude <= 0.5:
        raise SystemExit("--amplitude 必须在 (0, 0.5] 内")
    if args.countdown < 0:
        raise SystemExit("--countdown 不能为负")

    global CHIRP_AMPLITUDE
    CHIRP_AMPLITUDE = float(args.amplitude)
    try:
        input_device = resolve_device(args.input_device, input_device=True)
        output_device = resolve_device(args.output_device, input_device=False)
        input_info = device_metadata(input_device)
        output_info = device_metadata(output_device)
        output_channels = min(2, int(output_info["max_output_channels"]))
        if not 0 <= args.output_channel < output_channels:
            raise ValueError(
                f"--output-channel 必须在 [0, {output_channels - 1}] 内"
            )
        sd.check_input_settings(
            device=input_device, channels=INPUT_CHANNELS,
            samplerate=SAMPLE_RATE, dtype="float32",
        )
        sd.check_output_settings(
            device=output_device, channels=output_channels,
            samplerate=SAMPLE_RATE, dtype="float32",
        )
    except (ValueError, sd.PortAudioError) as exc:
        print(f"音频设备检查失败：{exc}", file=sys.stderr)
        return 2

    print(sd.query_devices())
    print("\nSelected devices:")
    print(f"  input : [{input_device}] {input_info['name']}")
    print(f"  output: [{output_device}] {output_info['name']}")
    print(f"  input channels: {INPUT_CHANNELS}")
    print(f"  output channel used: {args.output_channel}")
    print(f"  sample rate: {SAMPLE_RATE} Hz")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output_root.expanduser().resolve() / timestamp
    output.mkdir(parents=True, exist_ok=False)
    chirp = make_chirp()
    playback = make_playback(chirp)
    write_float_wav(output / "chirp.wav", chirp)
    write_float_wav(output / "playback_signal.wav", playback)

    print("\nPrepare for RIR capture.")
    print("Keep the room quiet.")
    print("Keep speaker and microphone stationary.")
    print(f"Capture starts in {args.countdown} seconds.", flush=True)
    for remaining in range(args.countdown, 0, -1):
        print(f"{remaining}...", flush=True)
        time.sleep(1.0)

    try:
        recording, stream_status = capture_duplex(
            playback,
            input_device=input_device,
            output_device=output_device,
            output_channels=output_channels,
            output_channel=args.output_channel,
        )
    except (OSError, RuntimeError, TimeoutError, sd.PortAudioError) as exc:
        print(f"真实采集失败：{exc}", file=sys.stderr)
        return 2

    np.save(output / "raw_recording.npy", recording)
    write_float_wav(output / "raw_recording.wav", recording)
    rirs, correlation_info, extraction_info = extract_rirs(recording, chirp)
    np.save(output / "rir_all_channels.npy", rirs)
    for channel in range(rirs.shape[1]):
        np.save(output / f"rir_ch{channel}.npy", rirs[:, channel])
    plot_rirs(output, rirs)

    peak_amplitudes = np.max(np.abs(recording), axis=0)
    clipped_counts = np.sum(np.abs(recording) >= 0.99, axis=0)
    clipped_total = int(clipped_counts.sum())
    active_correlation = correlation_info[:7]
    median_peak_to_noise_ratio = float(np.median([
        item["correlation_peak_to_noise_median_ratio"]
        for item in active_correlation
    ]))
    peak_lags = np.asarray([
        item["correlation_peak_lag_samples"] for item in active_correlation
    ])
    peak_lag_spread_ms = float((peak_lags.max() - peak_lags.min()) * 1000.0 / SAMPLE_RATE)
    quality_pass = bool(
        median_peak_to_noise_ratio >= 10.0
        and extraction_info["onset_candidate_spread_ms"] <= 10.0
    )
    metadata = {
        "capture_type": "real_room_rir",
        "timestamp_local": timestamp,
        "sample_rate": SAMPLE_RATE,
        "chirp_start_hz": CHIRP_START_HZ,
        "chirp_end_hz": CHIRP_END_HZ,
        "chirp_duration_s": CHIRP_DURATION_S,
        "chirp_kind": "linear (initial engineering implementation)",
        "chirp_amplitude": CHIRP_AMPLITUDE,
        "chirp_window": "Tukey alpha=0.1",
        "pre_silence_s": PRE_SILENCE_S,
        "post_silence_s": POST_SILENCE_S,
        "recording_duration_s": RECORDING_DURATION_S,
        "input_device": input_info,
        "output_device": output_info,
        "output_channel": args.output_channel,
        "channels": INPUT_CHANNELS,
        "recording_shape": list(recording.shape),
        "recording_dtype": str(recording.dtype),
        "peak_amplitudes": peak_amplitudes.tolist(),
        "clipped_samples_per_channel": clipped_counts.astype(int).tolist(),
        "clipped_samples_total": clipped_total,
        "stream_status": stream_status,
        "rir_shape": list(rirs.shape),
        "correlation": correlation_info,
        "extraction": extraction_info,
        "quality": {
            "pass": quality_pass,
            "median_correlation_peak_to_noise_ratio_ch0_ch6": median_peak_to_noise_ratio,
            "correlation_peak_lag_spread_ms_ch0_ch6": peak_lag_spread_ms,
            "criteria": {
                "minimum_median_peak_to_noise_ratio": 10.0,
                "maximum_onset_candidate_spread_ms": 10.0,
            },
        },
        "processing": {
            "agc": False,
            "aec": False,
            "noise_suppression": False,
            "beamforming": False,
            "automatic_normalization": False,
            "input_path": "PortAudio ALSA hw device converted from device PCM to float32",
        },
        "limitations": [
            "The time origin includes unknown playback/input buffering latency.",
            "Correlation peaks are candidates and are not asserted to be direct arrivals.",
            "This is matched filtering, not clock-synchronized absolute ToF.",
        ],
    }
    (output / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print("\nCapture diagnostics:")
    print(f"  recording duration: {RECORDING_DURATION_S:.3f} s")
    print(f"  recording shape: {recording.shape}")
    print(f"  peak amplitude per channel: {peak_amplitudes.tolist()}")
    print(f"  clipped samples per channel: {clipped_counts.astype(int).tolist()}")
    if clipped_total:
        print("  CLIPPING DETECTED - lower speaker volume or input gain")
    else:
        print("  clipping: none")
    for item in correlation_info:
        print(
            f"  CH{item['channel']} correlation peak: "
            f"{item['correlation_peak']:.7g} at "
            f"{item['correlation_peak_lag_ms']:.3f} ms (candidate only)"
        )
    print(f"  median correlation peak/noise ratio CH0-CH6: {median_peak_to_noise_ratio:.2f}")
    print(f"  correlation peak lag spread CH0-CH6: {peak_lag_spread_ms:.3f} ms")
    print(f"  RIR quality gate: {'PASS' if quality_pass else 'FAIL'}")
    print(f"  extracted RIR shape: {rirs.shape}")
    print(f"  output directory: {output}")
    if stream_status:
        print(f"  stream warnings: {stream_status}")
    if not quality_pass:
        print("  WARNING: no coherent real-room RIR was detected", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
