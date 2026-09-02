from __future__ import annotations

from datetime import datetime
from pathlib import Path
import secrets
import threading
from typing import Any, Callable

import numpy as np

from .audio_io import (
    capture_handshake,
    check_audio_configuration,
    output_warnings,
    resolve_device_info,
)
from .config import CHANNELS, SAMPLE_RATE, ControllerConfig
from .detector import analyze_recording
from .matched_filter import channel_status
from .plotting import write_all_plots
from .quality import assess_quality
from .result_writer import create_experiment_directory, write_experiment
from .rir import estimate_rirs
from .udp_listener import UdpListener, UdpMeasurementTracker
from .wav_utils import load_probe, wav_metadata


class Controller:
    def __init__(
        self,
        config: ControllerConfig,
        *,
        notify: Callable[[str], None] | None = None,
        audio_block: Callable[[np.ndarray], None] | None = None,
        rir_preview: Callable[[np.ndarray, bool], None] | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.config = config
        self._external_notify = notify or print
        self.audio_block = audio_block
        self.rir_preview = rir_preview
        self.stop_event = stop_event or threading.Event()
        self.log_lines: list[str] = []

    def notify(self, message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='milliseconds')} {message}"
        self.log_lines.append(line)
        self._external_notify(message)

    def run(self) -> tuple[Path, dict[str, Any]]:
        cfg = self.config
        cfg.validate()
        session_id = secrets.token_hex(8)
        c1, c1_warnings = load_probe(cfg.c1, warning=self.notify)
        c2, c2_warnings = load_probe(cfg.c2, warning=self.notify)
        c1_source = wav_metadata(cfg.c1)
        c2_source = wav_metadata(cfg.c2)
        directory = create_experiment_directory(cfg.output_root)
        self.notify("=== AV-Twin Linux Controller ===")
        input_device = resolve_device_info(cfg.input_device, input_device=True)
        output_device = resolve_device_info(cfg.output_device, input_device=False)
        check_audio_configuration(input_device, output_device, cfg.output_channel)
        input_index = input_device.portaudio_index
        output_index = output_device.portaudio_index
        input_info = input_device.to_dict()
        output_info = output_device.to_dict()
        self.notify("=== Audio configuration ===")
        self.notify(f"Sample rate: {SAMPLE_RATE} Hz (verified before playback)")
        self.notify("Input:")
        self.notify(f"  name: {input_device.display_name}")
        self.notify(f"  backend: {input_device.backend} / {input_device.hostapi}")
        self.notify(f"  ALSA: {input_device.alsa_stable_hw or input_device.alsa_hw or 'not available'}")
        if input_device.alsa_has_capture and input_device.alsa_stable_hw:
            self.notify("  capture path: direct ALSA arecord; matching PipeWire card profile is released for the recording")
        self.notify(
            f"  current PortAudio index: {input_index if input_index >= 0 else 'not used (direct ALSA)'}"
        )
        self.notify(f"  channels: {CHANNELS} of {input_device.max_input_channels}")
        self.notify("Output:")
        self.notify(f"  name: {output_device.display_name}")
        self.notify(f"  backend: {output_device.backend} / {output_device.hostapi}")
        self.notify(f"  ALSA: {output_device.alsa_stable_hw or output_device.alsa_hw or 'not available'}")
        if output_device.alsa_has_playback and output_device.alsa_stable_hw:
            self.notify("  playback path: direct ALSA aplay; matching PipeWire card profile is released only during playback")
        self.notify(
            f"  current PortAudio index: {output_index if output_index >= 0 else 'not used (direct ALSA)'}"
        )
        self.notify(f"  channels: 2 of {output_device.max_output_channels}; selected channel: {cfg.output_channel}")
        self.notify(
            f"  routing: C1 is emitted only by output channel {cfg.output_channel}; "
            "the other stereo channel remains silent"
        )
        for warning in output_warnings(output_device):
            self.notify(warning)
        for label, source, processed in (("C1", c1_source, c1), ("C2", c2_source, c2)):
            self.notify(f"{label}:")
            self.notify(f"  file: {source['path']}")
            self.notify(
                f"  source: {source['sample_rate']} Hz / {source['channels']} ch / "
                f"{source['duration_s']:.3f} s / {source['dtype']}"
            )
            self.notify(f"  internal template: {SAMPLE_RATE} Hz / mono / {processed.size / SAMPLE_RATE:.3f} s")
            if label == "C1":
                rendered_peak = min(1.0, float(np.max(np.abs(processed))) * cfg.playback_gain)
                self.notify(
                    f"  playback gain: {cfg.playback_gain:.3f}; rendered digital peak: "
                    f"{rendered_peak:.3f} FS"
                )

        udp = UdpListener(cfg.udp_host, cfg.udp_port, self.notify)
        tracker = UdpMeasurementTracker(session_id, compatibility_mode=cfg.udp_compatibility_mode)
        tracker.register(1)
        udp.start()
        arm_sent = False
        arm_error = None
        if cfg.android_host:
            try:
                udp.send_json(cfg.android_host, cfg.android_port, {
                    "type": "arm", "protocol_version": 1,
                    "session_id": session_id, "measurement_id": 1,
                })
                arm_sent = True
            except OSError as exc:
                arm_error = str(exc)
                self.notify(f"WARNING: ARM 发送失败，继续录音并等待 reply_timing：{exc}")

        def build_rir_preview(snapshot: np.ndarray, arrival_sample: int) -> None:
            if self.rir_preview is None:
                return
            preview, _info = estimate_rirs(
                snapshot,
                c2,
                arrival_sample,
                channel_status(snapshot),
                method=cfg.rir_method,
                duration=cfg.rir_duration,
                regularization=cfg.deconv_lambda,
                pre_arrival=cfg.rir_pre_arrival,
            )
            self.rir_preview(preview, False)

        try:
            capture = capture_handshake(
                c1,
                c2,
                input_device=input_device,
                output_device=output_device,
                output_channel=cfg.output_channel,
                playback_gain=cfg.playback_gain,
                pre_roll=cfg.pre_roll,
                reply_timeout=cfg.reply_timeout,
                tail=max(cfg.tail, cfg.rir_duration),
                c2_threshold=cfg.c2_threshold,
                stop_event=self.stop_event,
                notify=self.notify,
                audio_block=self.audio_block,
                recording_preview=build_rir_preview,
            )
        finally:
            udp.stop()

        for message in udp.messages:
            tracker.ingest(message)
        matched_android_messages = tracker.messages_for(1)
        tracker.complete(1)

        self.notify("正在分析连续 PCM 时间轴...")
        analysis = analyze_recording(
            capture.recording,
            c1,
            c2,
            playback_issue_sample=capture.playback_issue_sample,
            c1_threshold=cfg.c1_threshold,
            c2_threshold=cfg.c2_threshold,
            reply_timeout=cfg.reply_timeout,
            android_messages=matched_android_messages,
            speed_of_sound=cfg.speed_of_sound,
            linux_local_reference_correction=cfg.linux_local_reference_correction,
        )
        rirs, rir_info = estimate_rirs(
            capture.recording,
            c2,
            analysis["c2_detection"]["system_sample"],
            analysis["channel_status"],
            method=cfg.rir_method,
            duration=cfg.rir_duration,
            regularization=cfg.deconv_lambda,
            pre_arrival=cfg.rir_pre_arrival,
        )
        if self.rir_preview is not None:
            self.rir_preview(rirs, True)
        c1_pass = bool(analysis["c1_detection"]["passed"])
        c2_pass = bool(analysis["c2_detection"]["passed"])
        quality = assess_quality(
            capture.recording, rirs, analysis["c1_detection"], analysis["c2_detection"],
            direct_index=int(rir_info.get("direct_arrival_index", 0)),
            min_channels=cfg.min_detection_channels,
            tof_available=bool(analysis["tof"]["available"]),
            overall_policy=cfg.overall_policy,
        )
        overall = "INTERRUPTED" if capture.interrupted else quality["overall"]
        result: dict[str, Any] = {
            "session_id": session_id,
            "measurement_id": 1,
            "wall_clock_timestamp": datetime.now().astimezone().isoformat(),
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "input_device": input_info,
            "output_device": output_info,
            "output_channel": cfg.output_channel,
            "playback_gain": cfg.playback_gain,
            "c1_rendered_peak_fs": min(1.0, float(np.max(np.abs(c1))) * cfg.playback_gain),
            "playback_issue_sample_software_only": capture.playback_issue_sample,
            "live_c2_candidate_sample_non_authoritative": capture.live_c2_candidate_sample,
            "c1_file": str(cfg.c1.resolve()),
            "c2_file": str(cfg.c2.resolve()),
            "probe_warnings": c1_warnings + c2_warnings,
            **analysis,
            "t1_sample": analysis["c1_detection"]["system_sample"],
            "t4_sample": analysis["c2_detection"]["system_sample"],
            "exact_tof": analysis["tof"].get("tof_seconds", "NOT AVAILABLE"),
            "android": {
                "messages": udp.messages,
                "matched_messages": matched_android_messages,
                "association": tracker.association_for(1),
                "udp_received": bool(udp.messages),
                "listener_error": udp.error,
                "t3_precise": any(m.get("t3_precise") is True for m in matched_android_messages),
                "arm_sent": arm_sent,
                "arm_error": arm_error,
            },
            "rir": rir_info,
            "quality": {
                **quality,
                "c1_pass": c1_pass,
                "c2_pass": c2_pass,
                "overall": overall,
                "overall_pass": False if capture.interrupted else quality["overall_pass"],
            },
            "capture": {
                "frames": int(capture.recording.shape[0]),
                "duration_s": capture.recording.shape[0] / SAMPLE_RATE,
                "interrupted": capture.interrupted,
                "stream_warnings": capture.stream_warnings,
            },
            "limitations": [
                "Linux observed round-trip is measured only from acoustic events in one PCM timeline.",
                "Playback API call time is metadata and is not used as acoustic t1.",
                "No exact distance is reported unless Android supplies a precise reply delay.",
            ],
        }
        if c1_pass:
            self.notify(
                f"C1 locally detected: sample={analysis['c1_detection']['system_sample']} "
                f"score={analysis['c1_detection']['system_score']:.3f}"
            )
            self.notify(
                "C1 SENT: ACOUSTICALLY CONFIRMED by "
                f"{analysis['c1_detection']['channels_passed']} UMA-8 channels "
                "(not inferred from the playback API call)"
            )
        else:
            self.notify("C1 NOT FOUND")
        if c2_pass:
            self.notify(
                f"C2 received: sample={analysis['c2_detection']['system_sample']} "
                f"score={analysis['c2_detection']['system_score']:.3f}"
            )
        else:
            self.notify("C2 TIMEOUT / NOT FOUND")
        if udp.messages:
            self.notify(f"Android UDP: {len(udp.messages)} message(s) received")
        else:
            self.notify(
                "Android UDP: NONE — check whether the tablet detected C1; if it did, "
                "check Linux IP/UDP port 5005 and Wi-Fi connectivity"
            )
        if analysis["linux_observed_roundtrip_ms"] is not None:
            self.notify(f"Linux observed RTT: {analysis['linux_observed_roundtrip_ms']:.3f} ms (not final ToF)")
        if analysis["tof"]["available"]:
            self.notify(
                f"ToF distance: {analysis['tof']['distance_m']:.4f} m "
                f"({analysis['tof']['calibration']})"
            )
        else:
            self.notify(f"Exact ToF: NOT AVAILABLE — {analysis['tof']['reason']}")
        self.notify(f"Overall: {overall}")
        self.notify(f"Results: {directory}")
        write_experiment(
            directory, capture.recording, c1, c2, rirs, result, udp.raw_lines, self.log_lines
        )
        try:
            write_all_plots(directory / "plots", capture.recording, c1, c2, result, rirs)
        except Exception as exc:  # Results must survive a plotting/backend failure.
            self.notify(f"WARNING: 绘图失败：{exc}")
            (directory / "run.log").write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
        return directory, result
