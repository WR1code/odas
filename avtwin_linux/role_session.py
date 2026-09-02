from __future__ import annotations

from datetime import datetime, timezone
from collections import deque
from dataclasses import asdict
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .audio_io import ContinuousAudioBackend, output_warnings, resolve_device_info
from .config import CHANNELS, SAMPLE_RATE, ControllerConfig
from .continuous import PcmRingBuffer
from .handshake import HandshakeState, NetworkMetadata, Role
from .matched_filter import channel_status, detect_multichannel, normalized_correlation
from .output_paths import validate_output_root
from .pose import (
    ManualPoseProvider, NullPoseProvider, PoseProvider, UdpPoseProvider, transform_offset,
)
from .quality import assess_quality
from .rir import estimate_rirs
from .tof import calculate_tof
from .udp_listener import UdpListener
from .wav_utils import load_probe, wav_metadata, write_float32, write_pcm16


class ChirpDetector:
    """Shared per-channel detector for realtime triggering and precise analysis."""

    def __init__(self, threshold: float, min_channels: int):
        self.threshold = threshold
        self.min_channels = min_channels

    def detect(
        self, recording: np.ndarray, reference: np.ndarray, *, start: int = 0,
        stop: int | None = None,
    ) -> dict[str, Any]:
        result = detect_multichannel(
            recording, reference, self.threshold, channel_status(recording),
            start=start, stop=stop,
        )
        result["consensus_passed"] = result["channels_passed"] >= self.min_channels
        return result


class ChirpPlayer:
    """The probes are loaded before capture; play() only queues prepared PCM."""

    def __init__(self, backend: ContinuousAudioBackend):
        self.backend = backend

    def play(self, reference: np.ndarray) -> None:
        self.backend.play(reference)


class RIRExtractor:
    def __init__(self, config: ControllerConfig):
        self.config = config

    def extract(
        self, recording: np.ndarray, reference: np.ndarray, arrival: int | None,
        statuses: dict[str, str],
    ) -> tuple[np.ndarray, dict[str, Any]]:
        return estimate_rirs(
            recording, reference, arrival, statuses,
            method=self.config.rir_method,
            duration=self.config.rir_duration,
            regularization=self.config.deconv_lambda,
            pre_arrival=self.config.rir_pre_arrival,
        )


class AudioCaptureEngine:
    """One uninterrupted UMA-8 PCM timeline plus a bounded detection ring."""

    def __init__(
        self, backend: ContinuousAudioBackend, *, capacity_frames: int,
        audio_block: Callable[[np.ndarray], None] | None = None,
    ):
        self.backend = backend
        self.ring = PcmRingBuffer(capacity_frames, CHANNELS)
        self.blocks: list[np.ndarray] = []
        self.audio_block = audio_block
        self.progress = threading.Event()
        self.dropped_frames = 0
        self.warnings: list[str] = []
        self._clock_anchors: deque[tuple[int, int]] = deque(maxlen=4096)
        self._clock_lock = threading.Lock()

    @property
    def sample(self) -> int:
        return self.ring.end_sample

    def accept(self, block: np.ndarray, warning: str | None = None) -> None:
        values = np.ascontiguousarray(block, dtype=np.float32)
        _start, end = self.ring.append(values)
        received_ns = time.monotonic_ns()
        with self._clock_lock:
            self._clock_anchors.append((end, received_ns))
        self.blocks.append(values)
        if warning:
            self.warnings.append(warning)
        if self.audio_block:
            self.audio_block(values)
        self.progress.set()

    def recording(self) -> np.ndarray:
        return np.concatenate(self.blocks, axis=0) if self.blocks else np.zeros((0, CHANNELS), np.float32)

    def sample_timestamp(self, sample: int) -> tuple[int | None, dict[str, Any]]:
        """Map a PCM sample to local monotonic time using nearby block-receipt anchors."""
        with self._clock_lock:
            anchors = list(self._clock_anchors)
        if not anchors:
            return None, {"available": False, "reason": "audio clock has no block anchors"}
        anchor_sample, anchor_ns = min(anchors, key=lambda item: abs(item[0] - sample))
        offset_frames = int(sample) - anchor_sample
        timestamp_ns = anchor_ns + round(offset_frames * 1e9 / SAMPLE_RATE)
        return timestamp_ns, {
            "available": True,
            "basis": "CLOCK_MONOTONIC estimated from audio block receipt",
            "anchor_sample": anchor_sample,
            "anchor_monotonic_ns": anchor_ns,
            "offset_frames": offset_frames,
            "nominal_sample_rate": SAMPLE_RATE,
            "hardware_timestamp": False,
            "warning": "callback/ALSA delivery latency is not yet hardware timestamp calibrated",
        }


def _json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _role_directory(root: Path, role: Role) -> Path:
    root = validate_output_root(root, create=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = root / f"{stamp}_{role.value}"
    suffix = 1
    while candidate.exists():
        candidate = root / f"{stamp}_{role.value}_{suffix:02d}"
        suffix += 1
    for relative in ("raw", "references", "rir/remote", "rir/local", "analysis"):
        (candidate / relative).mkdir(parents=True, exist_ok=True)
    return candidate


def _save_rirs(directory: Path, rirs: np.ndarray, statuses: dict[str, str]) -> None:
    active = [index for index in range(rirs.shape[1]) if statuses.get(str(index)) == "active"]
    for channel in range(rirs.shape[1]):
        write_float32(directory / f"rir_ch{channel}.wav", rirs[:, channel])
    fused = np.median(rirs[:, active], axis=1).astype(np.float32) if active else np.zeros(rirs.shape[0], np.float32)
    write_float32(directory / "rir_fused.wav", fused)


def _save_correlations(path: Path, recording: np.ndarray, reference: np.ndarray) -> None:
    count = max(0, recording.shape[0] - reference.size + 1)
    values = np.zeros((count, recording.shape[1]), dtype=np.float32)
    for channel in range(recording.shape[1]):
        if count:
            values[:, channel] = normalized_correlation(recording[:, channel], reference).astype(np.float32)
    np.save(path, values)


def _shift(result: dict[str, Any], offset: int) -> dict[str, Any]:
    shifted = dict(result)
    for key in ("system_sample", "global_max_sample"):
        if shifted.get(key) is not None:
            shifted[key] = int(shifted[key]) + offset
    shifted["channels"] = {key: dict(value) for key, value in result["channels"].items()}
    for value in shifted["channels"].values():
        for key in ("sample", "global_max_sample"):
            if value.get(key) is not None:
                value[key] = int(value[key]) + offset
    return shifted


def _remote_duration(messages: list[dict[str, Any]], sample_rate: int) -> tuple[float | None, str | None]:
    for message in reversed(messages):
        try:
            if message.get("turnaround_samples") is not None:
                rate = int(message.get("sample_rate", sample_rate))
                value = float(message["turnaround_samples"]) / rate
                return (value, "turnaround_samples") if rate > 0 and value >= 0 else (None, None)
            if message.get("reply_delay_samples") is not None and message.get("t3_precise") is True:
                rate = int(message.get("sample_rate", sample_rate))
                value = float(message["reply_delay_samples"]) / rate
                return (value, "reply_delay_samples") if rate > 0 and value >= 0 else (None, None)
            if message.get("roundtrip_samples") is not None:
                rate = int(message.get("sample_rate", sample_rate))
                value = float(message["roundtrip_samples"]) / rate
                return (value, "roundtrip_samples") if rate > 0 and value >= 0 else (None, None)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return None, None


def _spatial_event(
    provider: PoseProvider,
    engine: AudioCaptureEngine,
    sample: int | None,
    config: ControllerConfig,
) -> dict[str, Any]:
    if sample is None:
        return {"available": False, "reason": "acoustic event was not detected"}
    timestamp_ns, audio_clock = engine.sample_timestamp(int(sample))
    result: dict[str, Any] = {
        "available": False,
        "audio_sample": int(sample),
        "audio_time_monotonic_ns": timestamp_ns,
        "audio_clock_mapping": audio_clock,
    }
    if timestamp_ns is None:
        result["reason"] = audio_clock["reason"]
        return result
    radar_pose, lookup = provider.pose_at(timestamp_ns)
    result["pose_lookup"] = lookup
    if radar_pose is None:
        result["reason"] = lookup["reason"]
        return result
    result.update({
        "available": True,
        "radar_pose": asdict(radar_pose),
        "speaker_pose": transform_offset(
            radar_pose, config.speaker_offset_m, child_frame_id="speaker_acoustic_center",
        ),
        "microphone_pose": transform_offset(
            radar_pose, config.microphone_offset_m, child_frame_id="uma8_acoustic_center",
        ),
    })
    return result


class HandshakeSession:
    """Single-shot AV-Twin state machine supporting both paper identities."""

    def __init__(
        self, config: ControllerConfig, *, notify: Callable[[str], None] | None = None,
        status: Callable[[dict[str, Any]], None] | None = None,
        audio_block: Callable[[np.ndarray], None] | None = None,
        rir_preview: Callable[[np.ndarray, bool], None] | None = None,
        stop_event: threading.Event | None = None,
        audio_backend: ContinuousAudioBackend | None = None,
        udp_listener: UdpListener | None = None,
        pose_provider: PoseProvider | None = None,
        session_id: str | None = None,
    ):
        self.config = config
        self.notify_external = notify or print
        self.status_callback = status
        self.audio_block = audio_block
        self.rir_preview = rir_preview
        self.stop_event = stop_event or threading.Event()
        self.audio_backend = audio_backend
        self.udp = udp_listener
        self.pose_provider = pose_provider
        self.session_id = session_id or secrets.token_hex(16)
        self.state = HandshakeState.IDLE
        self.events: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.debug_scores = {"c1": 0.0, "c2": 0.0}
        self.latest_spatial_events: dict[str, Any] = {}
        self._udp_ack_cursor = 0

    def notify(self, message: str) -> None:
        line = f"{datetime.now().astimezone().isoformat(timespec='milliseconds')} {message}"
        self.logs.append(line)
        self.notify_external(message)

    def _state(self, state: HandshakeState, engine: AudioCaptureEngine | None = None) -> None:
        self.state = state
        event = {
            "event": "state", "state": state.value,
            "sample": None if engine is None else engine.sample,
            "monotonic_ns": time.monotonic_ns(),
        }
        self.events.append(event)
        self.notify(f"STATE -> {state.value}")
        self._emit_status(engine)

    def _emit_status(self, engine: AudioCaptureEngine | None = None) -> None:
        if self.status_callback:
            latest_pose = None if self.pose_provider is None else self.pose_provider.latest()
            self.status_callback({
                "state": self.state.value, "role": self.config.role,
                "session_id": self.session_id,
                "audio_buffer_frames": None if engine is None else engine.ring.end_sample - engine.ring.start_sample,
                "dropped_frames": 0 if engine is None else engine.dropped_frames,
                "c1_threshold": self.config.c1_threshold,
                "c2_threshold": self.config.c2_threshold,
                "c1_score": self.debug_scores["c1"],
                "c2_score": self.debug_scores["c2"],
                "network_packets": 0 if self.udp is None else len(self.udp.messages),
                "playback_status": "ISSUED" if self.state in {HandshakeState.C1_PLAY, HandshakeState.C2_IMMEDIATE_RESPONSE} else "IDLE",
                "radar_pose": None if latest_pose is None else asdict(latest_pose),
                "speaker_pose": None if latest_pose is None else transform_offset(
                    latest_pose, self.config.speaker_offset_m,
                    child_frame_id="speaker_acoustic_center",
                ),
                "microphone_pose": None if latest_pose is None else transform_offset(
                    latest_pose, self.config.microphone_offset_m,
                    child_frame_id="uma8_acoustic_center",
                ),
                "latest_spatial_events": self.latest_spatial_events,
            })

    def _event(self, name: str, engine: AudioCaptureEngine, **values: Any) -> None:
        self.events.append({
            "event": name, "sample": engine.sample,
            "monotonic_ns": time.monotonic_ns(), **values,
        })

    def _wait_samples(self, engine: AudioCaptureEngine, target: int, input_session: Any) -> bool:
        while engine.sample < target and not self.stop_event.is_set():
            if input_session.finished.is_set():
                raise RuntimeError(input_session.errors[-1] if input_session.errors else "UMA-8 input ended")
            engine.progress.wait(0.05)
            engine.progress.clear()
        return not self.stop_event.is_set()

    def _stream_detect(
        self, engine: AudioCaptureEngine, detector: ChirpDetector,
        reference: np.ndarray, search_start: int, label: str,
    ) -> dict[str, Any] | None:
        end = engine.sample
        window_start = max(search_start, end - reference.size - round(0.30 * SAMPLE_RATE))
        offset, snapshot = engine.ring.read(window_start, end)
        if snapshot.shape[0] < reference.size + 8:
            return None
        result = detector.detect(snapshot, reference)
        self.debug_scores[label] = float(result.get("global_max_score", 0.0))
        if self.config.debug and self.status_callback:
            self._emit_status(engine)
        if not result["consensus_passed"]:
            return None
        return _shift(result, offset)

    def _send(self, message: dict[str, Any]) -> str | None:
        if not self.config.android_host:
            return None
        assert self.udp is not None
        try:
            self.udp.send_json(self.config.android_host, self.config.android_port, message)
            return None
        except OSError as exc:
            self.notify(f"WARNING: metadata send failed: {exc}")
            return str(exc)

    def _ack_remote_metadata(self) -> None:
        if self.udp is None:
            return
        messages = self.udp.messages
        for message in messages[self._udp_ack_cursor:]:
            if message.get("type") != "reply_timing" or not message.get("android_event_id"):
                continue
            accepted = (
                message.get("protocol_version") == 1
                and message.get("session_id") == self.session_id
                and message.get("measurement_id") == 1
            )
            error = self._send({
                "type": "reply_ack", "protocol_version": 1,
                "session_id": message.get("session_id"),
                "measurement_id": message.get("measurement_id"),
                "android_event_id": message.get("android_event_id"),
                "accepted": accepted,
                "reason": "accepted" if accepted else "session_or_measurement_mismatch",
                "receiver": "linux",
            })
            self.notify(
                f"REPLY_ACK_{'SENT' if error is None else 'FAILED'} "
                f"measurement={message.get('measurement_id')} "
                f"event={message.get('android_event_id')} accepted={accepted}"
            )
        self._udp_ack_cursor = len(messages)

    def _arm_android(self, engine: AudioCaptureEngine, input_session: Any) -> dict[str, Any]:
        if not self.config.android_host:
            return {"required": False, "accepted": True, "reason": "android_host_not_configured"}
        arm_event_id = secrets.token_hex(12)
        arm = {
            "type": "arm", "protocol_version": 1,
            "session_id": self.session_id, "measurement_id": 1,
            "arm_event_id": arm_event_id,
        }
        result: dict[str, Any] = {
            "required": True, "accepted": False, "arm_event_id": arm_event_id,
            "attempts": 0, "ack": None,
        }
        self._state(HandshakeState.WAIT_ARM_ACK, engine)
        for attempt in range(1, self.config.udp_ack_retries + 1):
            result["attempts"] = attempt
            error = self._send(arm)
            self.notify(
                f"ARM_SENT measurement=1 attempt={attempt}/{self.config.udp_ack_retries} "
                f"event={arm_event_id} error={error or 'none'}"
            )
            deadline = time.monotonic() + self.config.arm_ack_timeout
            while time.monotonic() < deadline and not self.stop_event.is_set():
                for message in self.udp.messages:
                    if (
                        message.get("type") == "arm_ack"
                        and message.get("protocol_version") == 1
                        and message.get("session_id") == self.session_id
                        and message.get("measurement_id") == 1
                        and message.get("arm_event_id") == arm_event_id
                    ):
                        result["ack"] = message
                        result["accepted"] = message.get("accepted") is True
                        result["reason"] = message.get("reason", "unknown")
                        self.notify(
                            f"ARM_ACK_{'ACCEPTED' if result['accepted'] else 'REJECTED'} "
                            f"measurement=1 reason={result['reason']}"
                        )
                        return result
                if input_session.finished.is_set():
                    raise RuntimeError(
                        input_session.errors[-1] if input_session.errors else "UMA-8 input ended"
                    )
                engine.progress.wait(0.01)
                engine.progress.clear()
        result["reason"] = f"timeout_after_{self.config.udp_ack_retries}_attempts"
        self.notify("ARM_ACK_TIMEOUT measurement=1")
        return result

    def run(self) -> tuple[Path, dict[str, Any]]:
        cfg = self.config
        cfg.validate()
        role = Role(cfg.role)
        c1, c1_warnings = load_probe(cfg.c1, warning=self.notify)
        c2, c2_warnings = load_probe(cfg.c2, warning=self.notify)
        # Loading both probes here is intentional: responder never opens/decodes C2 after C1 detection.
        input_info = resolve_device_info(cfg.input_device, input_device=True)
        output_info = resolve_device_info(cfg.output_device, input_device=False)
        self.audio_backend = self.audio_backend or ContinuousAudioBackend(
            input_info, output_info, cfg.output_channel, cfg.playback_gain,
        )
        player = ChirpPlayer(self.audio_backend)
        c1_detector = ChirpDetector(cfg.c1_threshold, cfg.min_detection_channels)
        c2_detector = ChirpDetector(cfg.c2_threshold, cfg.min_detection_channels)
        extractor = RIRExtractor(cfg)
        capacity = round(max(15.0, cfg.pre_roll + cfg.reply_timeout + cfg.tail + cfg.rir_duration + 2.0) * SAMPLE_RATE)
        engine = AudioCaptureEngine(self.audio_backend, capacity_frames=capacity, audio_block=self.audio_block)
        directory = _role_directory(cfg.output_root, role)
        write_pcm16(directory / "references" / "c1.wav", c1)
        write_pcm16(directory / "references" / "c2.wav", c2)
        self.udp = self.udp or UdpListener(cfg.udp_host, cfg.udp_port, self.notify)
        if self.pose_provider is None:
            if cfg.pose_source == "udp":
                self.pose_provider = UdpPoseProvider(
                    cfg.pose_udp_host, cfg.pose_udp_port, cfg.pose_max_age,
                )
            elif cfg.pose_source == "manual":
                self.pose_provider = ManualPoseProvider(cfg.manual_position_m)
            else:
                self.pose_provider = NullPoseProvider()
        for warning in output_warnings(output_info):
            self.notify(warning)

        realtime: dict[str, Any] = {}
        timestamps: dict[str, Any] = {"t1": None, "t2": None, "t3": None, "t4": None}
        playback_clock: dict[str, Any] = {}
        metadata_send_errors: list[str] = []
        failure_reasons: list[str] = []
        arm_result: dict[str, Any] = {
            "required": False, "accepted": True, "reason": "not_initiator",
        }
        self.pose_provider.start()
        try:
            self.udp.start()
            try:
                self._state(HandshakeState.INIT_RECORDING, engine)
                with self.audio_backend.capture(engine.accept) as input_session:
                    self._state(HandshakeState.PRE_ROLL, engine)
                    pre_start = engine.sample
                    self._wait_samples(engine, pre_start + round(cfg.pre_roll * SAMPLE_RATE), input_session)
                    listen_start = engine.sample
                    if role is Role.INITIATOR and not self.stop_event.is_set():
                        arm_result = self._arm_android(engine, input_session)
                        if not arm_result["accepted"]:
                            failure_reasons.append(
                                f"ARM not acknowledged/accepted: {arm_result.get('reason', 'unknown')}"
                            )
                            self._state(HandshakeState.POST_ROLL, engine)
                            self._wait_samples(
                                engine, engine.sample + round(max(cfg.tail, 0.05) * SAMPLE_RATE),
                                input_session,
                            )
                            continue_after_initiator = False
                        else:
                            continue_after_initiator = True
                    else:
                        continue_after_initiator = False
                    if role is Role.INITIATOR and continue_after_initiator and not self.stop_event.is_set():
                        self._state(HandshakeState.C1_PLAY, engine)
                        playback_clock["c1_playback_requested_sample"] = engine.sample
                        playback_clock["c1_playback_requested_monotonic_ns"] = time.monotonic_ns()
                        self.notify("C1 PLAYBACK ISSUED")
                        player.play(c1)
                        self._event("c1_playback_requested", engine, **playback_clock)
                        self._state(HandshakeState.WAIT_C2, engine)
                        self._send(NetworkMetadata(
                            "AVTWIN_V1", self.session_id, role.value, self.state.value,
                            "audio_sample_index", {"type": "handshake_state", "sample_rate": SAMPLE_RATE},
                        ).to_dict())
                        deadline = time.monotonic() + cfg.reply_timeout
                        c2_search = playback_clock["c1_playback_requested_sample"] + c1.size
                        while time.monotonic() < deadline and not self.stop_event.is_set():
                            self._ack_remote_metadata()
                            if "c1" not in realtime:
                                found = self._stream_detect(engine, c1_detector, c1, playback_clock["c1_playback_requested_sample"], "c1")
                                if found:
                                    realtime["c1"] = found
                                    self._event("c1_local_acoustically_confirmed", engine, detection=found)
                                    self.notify(f"C1 ACOUSTICALLY CONFIRMED sample={found['system_sample']} score={found['system_score']:.3f}")
                                    c2_search = int(found["system_sample"]) + c1.size
                            found = self._stream_detect(engine, c2_detector, c2, c2_search, "c2")
                            if found:
                                realtime["c2"] = found
                                self._event("c2_detected_realtime", engine, detection=found)
                                timestamps["t4_realtime_sample"] = found["system_sample"]
                                self._state(HandshakeState.C2_DETECTED, engine)
                                self.notify(f"C2 DETECTED realtime sample={found['system_sample']} score={found['system_score']:.3f}")
                                break
                            self._wait_samples(engine, engine.sample + 1, input_session)
                        if "c2" not in realtime:
                            failure_reasons.append("C2 timeout / acoustic detection failed")
                        self._state(HandshakeState.POST_ROLL, engine)
                        self._wait_samples(engine, engine.sample + round(max(cfg.tail, cfg.rir_duration) * SAMPLE_RATE), input_session)
                        self._ack_remote_metadata()
                    elif role is Role.RESPONDER and not self.stop_event.is_set():
                        self._state(HandshakeState.LISTEN_C1, engine)
                        deadline = time.monotonic() + cfg.reply_timeout
                        while time.monotonic() < deadline and not self.stop_event.is_set():
                            found = self._stream_detect(engine, c1_detector, c1, listen_start, "c1")
                            if found:
                                # No UI, network, disk, or RIR work is permitted before C2 is queued.
                                realtime["c1"] = found
                                timestamps["t2_realtime_sample"] = int(found["system_sample"])
                                timestamps["t2_realtime_monotonic_ns"] = time.monotonic_ns()
                                self.state = HandshakeState.C1_DETECTED
                                playback_clock["c2_playback_requested_sample"] = engine.sample
                                playback_clock["c2_playback_requested_monotonic_ns"] = time.monotonic_ns()
                                player.play(c2)
                                self._event(
                                    "c2_playback_requested_immediately_after_c1", engine,
                                    t2_realtime_sample=timestamps["t2_realtime_sample"],
                                    t3_playback_enqueue_sample=playback_clock["c2_playback_requested_sample"],
                                )
                                timestamps["t3_playback_enqueue_sample"] = playback_clock["c2_playback_requested_sample"]
                                timestamps["t3_playback_enqueue_monotonic_ns"] = playback_clock["c2_playback_requested_monotonic_ns"]
                                self._state(HandshakeState.C2_IMMEDIATE_RESPONSE, engine)
                                self.notify(
                                    f"C1 DETECTED t2={timestamps['t2_realtime_sample']}; C2 PLAYBACK ISSUED "
                                    f"t3_enqueue={timestamps['t3_playback_enqueue_sample']}"
                                )
                                turnaround = timestamps["t3_playback_enqueue_sample"] - timestamps["t2_realtime_sample"]
                                immediate_send_error = self._send(NetworkMetadata(
                                    "AVTWIN_V1", self.session_id, role.value, self.state.value,
                                    "audio_sample_index_estimated_at_playback_enqueue",
                                    {
                                        "type": "reply_timing", "sample_rate": SAMPLE_RATE,
                                        "c1_detected": True, "t2_sample": timestamps["t2_realtime_sample"],
                                        "c2_played": True, "t3_sample": timestamps["t3_playback_enqueue_sample"],
                                        "turnaround_samples": turnaround,
                                        "turnaround_seconds": turnaround / SAMPLE_RATE,
                                        "c1_score": found["system_score"],
                                        "t3_precise": False,
                                    },
                                ).to_dict())
                                if immediate_send_error:
                                    metadata_send_errors.append(immediate_send_error)
                                break
                            self._wait_samples(engine, engine.sample + 1, input_session)
                        if "c1" not in realtime:
                            failure_reasons.append("C1 timeout / detector rejected signal")
                        self._state(HandshakeState.POST_ROLL, engine)
                        target = engine.sample + round(max(cfg.tail, cfg.rir_duration) * SAMPLE_RATE)
                        if playback_clock.get("c2_playback_requested_sample") is not None:
                            target = max(target, int(playback_clock["c2_playback_requested_sample"]) + c2.size + round(max(cfg.tail, cfg.rir_duration) * SAMPLE_RATE))
                        self._wait_samples(engine, target, input_session)
            finally:
                self._ack_remote_metadata()
                self.udp.stop()
        finally:
            self.pose_provider.stop()

        recording = engine.recording()
        write_float32(directory / "raw" / "uma8_8ch.wav", recording)
        statuses = channel_status(recording)
        inactive = [int(key) for key, value in statuses.items() if value == "inactive_zero"]
        valid = [int(key) for key, value in statuses.items() if value == "active"]
        self._state(HandshakeState.PRECISE_ANALYSIS, engine)
        if role is Role.INITIATOR:
            issue = int(playback_clock.get("c1_playback_requested_sample", 0))
            c1_precise = c1_detector.detect(recording, c1, start=max(0, issue - round(0.02 * SAMPLE_RATE)), stop=min(recording.shape[0], issue + c1.size + round(0.5 * SAMPLE_RATE)))
            c2_start = (int(c1_precise["system_sample"]) + c1.size) if c1_precise["system_sample"] is not None else issue + c1.size
            c2_precise = c2_detector.detect(recording, c2, start=c2_start)
            timestamps["t1"] = c1_precise["system_sample"]
            timestamps["t4"] = c2_precise["system_sample"]
            remote_reference, remote_detection = c2, c2_precise
            local_reference, local_detection = c1, c1_precise
        else:
            c1_precise = c1_detector.detect(recording, c1, start=listen_start)
            issue = int(playback_clock.get("c2_playback_requested_sample", recording.shape[0]))
            c2_precise = c2_detector.detect(recording, c2, start=max(0, issue - round(0.02 * SAMPLE_RATE)), stop=min(recording.shape[0], issue + c2.size + round(0.5 * SAMPLE_RATE)))
            timestamps["t2"] = c1_precise["system_sample"]
            timestamps["t3"] = c2_precise["system_sample"]
            remote_reference, remote_detection = c1, c1_precise
            local_reference, local_detection = c2, c2_precise
        local_event_names = ("t1", "t4") if role is Role.INITIATOR else ("t2", "t3")
        spatial_events = {
            name: _spatial_event(self.pose_provider, engine, timestamps[name], cfg)
            for name in local_event_names
        }
        self.latest_spatial_events = spatial_events
        available_positions = sum(bool(value.get("available")) for value in spatial_events.values())
        if cfg.pose_source != "disabled":
            self.notify(
                f"空间坐标标注：{available_positions}/{len(spatial_events)} 个本地声学事件获得位姿"
            )
        self._state(HandshakeState.RIR_EXTRACTION, engine)
        remote_rirs, remote_info = extractor.extract(recording, remote_reference, remote_detection["system_sample"], statuses)
        local_rirs, local_info = extractor.extract(recording, local_reference, local_detection["system_sample"], statuses)
        _save_rirs(directory / "rir" / "remote", remote_rirs, statuses)
        _save_rirs(directory / "rir" / "local", local_rirs, statuses)
        if self.rir_preview:
            self.rir_preview(remote_rirs, True)
        try:
            _save_correlations(directory / "analysis" / "c1_correlation.npy", recording, c1)
            _save_correlations(directory / "analysis" / "c2_correlation.npy", recording, c2)
        except Exception as exc:
            failure_reasons.append(f"correlation save failed: {exc}")

        self._state(HandshakeState.TOF_CALCULATION, engine)
        all_messages = list(self.udp.messages)
        messages = [message for message in all_messages if message.get("session_id") == self.session_id]
        if role is Role.INITIATOR:
            local_roundtrip = None if timestamps["t1"] is None or timestamps["t4"] is None else int(timestamps["t4"] - timestamps["t1"])
            tof = calculate_tof(local_roundtrip, SAMPLE_RATE, messages, cfg.speed_of_sound, cfg.linux_local_reference_correction)
        else:
            local_turnaround = None if timestamps["t2"] is None or timestamps["t3"] is None else int(timestamps["t3"] - timestamps["t2"])
            remote_roundtrip_s, remote_source = _remote_duration(messages, SAMPLE_RATE)
            if local_turnaround is None:
                tof = {"available": False, "reason": "precise local t2/t3 acoustic detections are incomplete", "exact_tof": "NOT AVAILABLE"}
            elif remote_roundtrip_s is None:
                tof = {"available": False, "reason": "remote initiator t1/t4 round-trip metadata is missing", "exact_tof": "NOT AVAILABLE"}
            else:
                corrected = remote_roundtrip_s - local_turnaround / SAMPLE_RATE
                if corrected < 0:
                    tof = {"available": False, "reason": "corrected round-trip time is negative", "exact_tof": "NOT AVAILABLE"}
                else:
                    tof_s = corrected / 2.0
                    tof = {"available": True, "tof_seconds": tof_s, "distance_m": tof_s * cfg.speed_of_sound, "remote_roundtrip_source": remote_source}

        self._state(HandshakeState.SEND_METADATA, engine)
        if role is Role.RESPONDER:
            precise_turnaround = (
                None if timestamps["t2"] is None or timestamps["t3"] is None
                else int(timestamps["t3"] - timestamps["t2"])
            )
            final_payload = {
                "type": "reply_timing", "sample_rate": SAMPLE_RATE,
                "c1_detected": timestamps["t2"] is not None, "t2_sample": timestamps["t2"],
                "c2_played": timestamps["t3"] is not None, "t3_sample": timestamps["t3"],
                "turnaround_samples": precise_turnaround,
                "turnaround_seconds": None if precise_turnaround is None else precise_turnaround / SAMPLE_RATE,
                "reply_delay_samples": precise_turnaround,
                "c1_score": c1_precise["system_score"],
                "t3_precise": precise_turnaround is not None,
                "local_spatial_events": spatial_events,
            }
        else:
            precise_roundtrip = (
                None if timestamps["t1"] is None or timestamps["t4"] is None
                else int(timestamps["t4"] - timestamps["t1"])
            )
            final_payload = {
                "type": "initiator_timing", "sample_rate": SAMPLE_RATE,
                "t1_sample": timestamps["t1"], "t4_sample": timestamps["t4"],
                "roundtrip_samples": precise_roundtrip,
                "roundtrip_seconds": None if precise_roundtrip is None else precise_roundtrip / SAMPLE_RATE,
                "c2_score": c2_precise["system_score"],
                "timing_precise": precise_roundtrip is not None,
                "local_spatial_events": spatial_events,
            }
        final_send_error = self._send(NetworkMetadata(
            "AVTWIN_V1", self.session_id, role.value, self.state.value,
            "audio_sample_index", final_payload,
        ).to_dict())
        if final_send_error:
            metadata_send_errors.append(final_send_error)
        self._event("precise_metadata_send", engine, payload=final_payload, error=final_send_error)

        if self.stop_event.is_set():
            failure_reasons.append("session interrupted by user")
        if role is Role.INITIATOR and c1_precise["system_sample"] is None:
            failure_reasons.append("C1 local acoustic confirmation failed")
        if role is Role.INITIATOR and c2_precise["system_sample"] is None:
            failure_reasons.append("C2 acoustic detection failed")
        if role is Role.RESPONDER and c1_precise["system_sample"] is None:
            failure_reasons.append("C1 precise detector rejected signal")
        if role is Role.RESPONDER and playback_clock.get("c2_playback_requested_sample") is None:
            failure_reasons.append("C2 playback was not issued")
        if role is Role.RESPONDER and c2_precise["system_sample"] is None:
            failure_reasons.append("C2 local acoustic confirmation failed")
        if metadata_send_errors:
            failure_reasons.append("metadata send failed")
        failure_reasons = list(dict.fromkeys(failure_reasons))

        quality = assess_quality(
            recording, remote_rirs, c1_precise, c2_precise,
            direct_index=int(remote_info.get("direct_arrival_index", 0)),
            min_channels=cfg.min_detection_channels,
            tof_available=bool(tof.get("available")), overall_policy=cfg.overall_policy,
        )
        acoustic_success = c1_precise["channels_passed"] >= cfg.min_detection_channels and c2_precise["channels_passed"] >= cfg.min_detection_channels
        result_status = "SUCCESS" if acoustic_success and not failure_reasons else "FAIL"
        final_state = HandshakeState.DONE if result_status == "SUCCESS" else HandshakeState.FAILED
        self._state(final_state, engine)
        metadata: dict[str, Any] = {
            "protocol": "AVTWIN_V1", "session_id": self.session_id,
            "role": role.value.upper(), "role_display": role.display_name,
            "sample_rate": SAMPLE_RATE, "timestamp_basis": "audio_sample_index",
            "playback_enqueue_timestamp_basis": "audio_sample_index_estimated_at_playback_enqueue",
            "input_device": input_info.to_dict(), "output_device": output_info.to_dict(),
            "output_channel": cfg.output_channel, "playback_gain": cfg.playback_gain,
            "c1_file": str(cfg.c1.resolve()), "c2_file": str(cfg.c2.resolve()),
            "c1_reference": wav_metadata(cfg.c1), "c2_reference": wav_metadata(cfg.c2),
            "record_start": self.events[0] if self.events else None,
            **timestamps, **playback_clock,
            "turnaround_samples": (
                None if role is Role.INITIATOR or timestamps["t2"] is None or timestamps["t3"] is None
                else int(timestamps["t3"] - timestamps["t2"])
            ),
            "tof": tof, "distance": tof.get("distance_m"),
            "valid_channels": valid, "inactive_channels": inactive,
            "c1_scores": c1_precise, "c2_scores": c2_precise,
            "direct_path_peaks": {"c1": c1_precise.get("system_sample"), "c2": c2_precise.get("system_sample")},
            "global_max_peaks": {"c1": c1_precise.get("global_max_sample"), "c2": c2_precise.get("global_max_sample")},
            "rir_length_seconds": cfg.rir_duration,
            "remote_rir": remote_info, "local_rir": local_info,
            "network_packets": all_messages, "matched_network_packets": messages,
            "network_listener_error": self.udp.error,
            "arm_handshake": arm_result,
            "metadata_send_errors": metadata_send_errors,
            "pose_interface": self.pose_provider.metadata(),
            "pose_extrinsics": {
                "convention": "offset expressed in lidar child frame; world = radar_position + R(radar_quaternion) * offset",
                "speaker_offset_m": list(cfg.speaker_offset_m),
                "microphone_offset_m": list(cfg.microphone_offset_m),
            },
            "local_spatial_events": spatial_events,
            "dropped_frames": engine.dropped_frames, "audio_warnings": engine.warnings,
            "probe_warnings": c1_warnings + c2_warnings,
            "realtime_detection": realtime, "quality": quality,
            "result": result_status, "failure_reason": failure_reasons,
        }
        _json(directory / "analysis" / "peaks.json", {"c1": c1_precise, "c2": c2_precise})
        _json(directory / "metadata.json", metadata)
        _json(directory / "events.json", self.events)
        (directory / "log.txt").write_text("\n".join(self.logs) + "\n", encoding="utf-8")
        self.notify(f"{role.display_name}: {result_status}; results={directory}")
        return directory, metadata
