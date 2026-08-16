from __future__ import annotations

from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable, Protocol

import numpy as np

from .audio_io import (
    ContinuousAudioBackend,
    output_warnings,
    resolve_device_info,
)
from .config import CHANNELS, SAMPLE_RATE, ControllerConfig
from .detector import analyze_recording
from .matched_filter import channel_status, detect_multichannel
from .plotting import write_all_plots
from .pose import (
    AudioSampleClock, ManualPoseProvider, NullPoseProvider, PoseProvider, UdpPoseProvider,
    transform_offset,
)
from .quality import assess_quality
from .result_writer import SessionWriter
from .rir import estimate_rirs
from .udp_listener import UdpListener, UdpMeasurementTracker
from .wav_utils import load_probe, wav_metadata


class CaptureState(str, Enum):
    IDLE = "IDLE"
    ARMED = "ARMED"
    WAIT_ARM_ACK = "WAIT_ARM_ACK"
    C1_PLAYING = "C1_PLAYING"
    WAIT_C2 = "WAIT_C2"
    CAPTURE_TAIL = "CAPTURE_TAIL"
    FINALIZE = "FINALIZE"


class AudioBackend(Protocol):
    def capture(self, accept_block: Callable[[np.ndarray, str | None], None]): ...
    def play(self, probe: np.ndarray) -> None: ...


class PcmRingBuffer:
    def __init__(self, capacity_frames: int, channels: int = CHANNELS):
        self.capacity_frames = max(1, capacity_frames)
        self.channels = channels
        self._blocks: deque[tuple[int, np.ndarray]] = deque()
        self._end = 0
        self._lock = threading.Lock()

    @property
    def end_sample(self) -> int:
        with self._lock:
            return self._end

    @property
    def start_sample(self) -> int:
        with self._lock:
            return self._blocks[0][0] if self._blocks else self._end

    def append(self, block: np.ndarray) -> tuple[int, int]:
        values = np.ascontiguousarray(block, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != self.channels:
            raise ValueError("输入 PCM block 必须是 8 通道二维数组")
        with self._lock:
            start = self._end
            self._end += values.shape[0]
            self._blocks.append((start, values))
            cutoff = self._end - self.capacity_frames
            while self._blocks and self._blocks[0][0] + self._blocks[0][1].shape[0] <= cutoff:
                self._blocks.popleft()
            return start, self._end

    def read(self, start: int, stop: int) -> tuple[int, np.ndarray]:
        with self._lock:
            actual_start = max(start, self._blocks[0][0] if self._blocks else self._end)
            actual_stop = min(stop, self._end)
            if actual_stop <= actual_start:
                return actual_start, np.zeros((0, self.channels), dtype=np.float32)
            pieces: list[np.ndarray] = []
            for block_start, block in self._blocks:
                block_stop = block_start + block.shape[0]
                left = max(actual_start, block_start)
                right = min(actual_stop, block_stop)
                if right > left:
                    pieces.append(block[left - block_start:right - block_start])
            return actual_start, np.concatenate(pieces, axis=0) if pieces else np.zeros((0, self.channels), np.float32)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _shift_detection(detection: dict[str, Any], offset: int) -> dict[str, Any]:
    shifted = {**detection, "channels": {}}
    if shifted.get("system_sample") is not None:
        shifted["system_sample"] = int(shifted["system_sample"]) + offset
        shifted["system_time_ms"] = shifted["system_sample"] * 1000.0 / SAMPLE_RATE
    for channel, values in detection.get("channels", {}).items():
        copied = dict(values)
        if copied.get("sample") is not None:
            copied["sample"] = int(copied["sample"]) + offset
        shifted["channels"][channel] = copied
    return shifted


class ContinuousController:
    def __init__(
        self,
        config: ControllerConfig,
        *,
        notify: Callable[[str], None] | None = None,
        status: Callable[[dict[str, Any]], None] | None = None,
        audio_block: Callable[[np.ndarray], None] | None = None,
        rir_preview: Callable[[np.ndarray, bool], None] | None = None,
        stop_event: threading.Event | None = None,
        audio_backend: AudioBackend | None = None,
        udp_listener: UdpListener | None = None,
        pose_provider: PoseProvider | None = None,
        session_id: str | None = None,
    ):
        self.config = config
        self._external_notify = notify or print
        self.status_callback = status
        self.audio_block = audio_block
        self.rir_preview = rir_preview
        self.stop_event = stop_event or threading.Event()
        self.audio_backend = audio_backend
        self.udp_listener = udp_listener
        self.pose_provider = pose_provider
        self.session_id = session_id
        self.state = CaptureState.IDLE
        self.pause_event = threading.Event()
        self._manual_requests = 0
        self._command_lock = threading.Lock()
        self._progress = threading.Event()
        self._writer: SessionWriter | None = None
        self._log_lines: list[str] = []
        self._latest_quality: dict[str, Any] | None = None
        self.success_count = 0
        self.failure_count = 0
        self.skip_count = 0
        self.skip_events: list[dict[str, Any]] = []
        self.measurement_id = 0
        self.next_due_sample: int | None = None
        self._latest_spatial_events: dict[str, Any] = {}
        self._udp_cursor = 0
        self._capture_request_results: dict[str, tuple[bool, str, str, int | None]] = {}
        self._capture_request_order: deque[str] = deque()

    def notify(self, message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='milliseconds')} {message}"
        self._log_lines.append(line)
        if self._writer is not None:
            self._writer.append_log(line)
        self._external_notify(message)

    def request_capture(self) -> bool:
        with self._command_lock:
            if self.state != CaptureState.ARMED or self.pause_event.is_set():
                self.skip_count += 1
                self.skip_events.append({
                    "sample": self._ring.end_sample if hasattr(self, "_ring") else None,
                    "reason": "manual_request_while_unavailable",
                    "state": self.state.value,
                })
                self.notify(f"采集请求已忽略：当前状态 {self.state.value}")
                return False
            self._manual_requests += 1
        self._progress.set()
        return True

    def pause(self) -> None:
        self.pause_event.set()
        self.notify("自动采集已暂停；当前轮次仍会安全完成")
        self._emit_status()

    def resume(self) -> None:
        self.pause_event.clear()
        if self.config.capture_mode == "timed_continuous" and self.next_due_sample is not None:
            # A paused interval is skipped, never replayed as a burst on resume.
            self.next_due_sample = max(self.next_due_sample, getattr(self, "_ring", PcmRingBuffer(1)).end_sample)
        self.notify("自动采集已继续")
        self._progress.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.notify("收到安全停止请求")
        self._progress.set()

    def _set_state(self, state: CaptureState) -> None:
        self.state = state
        self.notify(f"STATE -> {state.value}")
        self._emit_status()

    def _emit_status(self) -> None:
        if self.status_callback is None:
            return
        current = self._ring.end_sample if hasattr(self, "_ring") else 0
        countdown = None if self.next_due_sample is None else max(0.0, (self.next_due_sample - current) / SAMPLE_RATE)
        latest_pose = None if self.pose_provider is None else self.pose_provider.latest()
        self.status_callback({
            "state": self.state.value,
            "measurement_id": self.measurement_id,
            "success": self.success_count,
            "failure": self.failure_count,
            "skipped": self.skip_count,
            "next_trigger_seconds": countdown,
            "paused": self.pause_event.is_set(),
            "latest_quality": self._latest_quality,
            "radar_pose": None if latest_pose is None else asdict(latest_pose),
            "speaker_pose": None if latest_pose is None else transform_offset(
                latest_pose, self.config.speaker_offset_m,
                child_frame_id="speaker_acoustic_center",
            ),
            "microphone_pose": None if latest_pose is None else transform_offset(
                latest_pose, self.config.microphone_offset_m,
                child_frame_id="uma8_acoustic_center",
            ),
            "latest_spatial_events": self._latest_spatial_events,
        })

    def _ingest_udp(self, tracker: UdpMeasurementTracker) -> None:
        assert self.udp_listener is not None
        messages = self.udp_listener.messages
        for message in messages[self._udp_cursor:]:
            if message.get("type") == "capture_once_request":
                self._handle_capture_once_request(message)
                continue
            event = tracker.ingest(message)
            if message.get("type") == "reply_timing" and message.get("android_event_id"):
                accepted = event["status"] in {"accepted", "duplicate"}
                if self.config.android_host:
                    try:
                        self.udp_listener.send_json(
                            self.config.android_host, self.config.android_port,
                            {
                                "type": "reply_ack", "protocol_version": 1,
                                "session_id": message.get("session_id"),
                                "measurement_id": message.get("measurement_id"),
                                "android_event_id": message.get("android_event_id"),
                                "accepted": accepted,
                                "reason": "accepted" if accepted else event["status"],
                                "receiver": "linux",
                            },
                        )
                        self.notify(
                            f"REPLY_ACK_SENT measurement={message.get('measurement_id')} "
                            f"event={message.get('android_event_id')} accepted={accepted}"
                        )
                    except OSError as exc:
                        self.notify(f"WARNING: REPLY_ACK 发送失败：{exc}")
            if event["status"] == "arm_ack_accepted":
                self.notify(
                    f"ARM_ACK_ACCEPTED measurement={event.get('measurement_id')} "
                    f"reason={event.get('reason')}"
                )
            elif event["status"] == "arm_ack_rejected":
                self.notify(
                    f"ARM_ACK_REJECTED measurement={event.get('measurement_id')} "
                    f"reason={event.get('reason')}"
                )
            elif event["status"] != "accepted":
                self.notify(f"UDP {event['status']}：measurement_id={message.get('measurement_id')}")
        self._udp_cursor = len(messages)
        if self._writer:
            self._writer.append_udp(self.udp_listener.raw_lines)

    def _handle_capture_once_request(self, message: dict[str, Any]) -> None:
        assert self.udp_listener is not None
        request_id = message.get("request_id")
        source_host = str(message.get("source", "")).rsplit(":", 1)[0]
        expected_host = self.config.android_host
        if not isinstance(request_id, str) or not request_id:
            self.notify("IOS_CAPTURE_ONCE_REJECTED：缺少 request_id")
            return
        if expected_host and source_host != expected_host:
            self.notify(
                f"IOS_CAPTURE_ONCE_REJECTED request={request_id}："
                f"来源 {source_host} 与配置的移动端 {expected_host} 不一致"
            )
            return

        cached = self._capture_request_results.get(request_id)
        if cached is None:
            requested_port = message.get("ios_control_port")
            if message.get("protocol_version") != 1:
                result = (False, self.state.value, "unsupported_protocol_version", None)
            elif requested_port != self.config.android_port:
                result = (False, self.state.value, "ios_control_port_mismatch", None)
            else:
                accepted = self.request_capture()
                result = (
                    accepted,
                    self.state.value,
                    "accepted_queued" if accepted else (
                        "automatic_capture_paused" if self.pause_event.is_set()
                        else "capture_session_busy_or_unavailable"
                    ),
                    None,
                )
            self._capture_request_results[request_id] = result
            self._capture_request_order.append(request_id)
            if len(self._capture_request_order) > 256:
                self._capture_request_results.pop(self._capture_request_order.popleft(), None)
        else:
            result = cached

        accepted, state, reason, measurement_id = result
        acknowledgement = {
            "type": "capture_once_ack", "protocol_version": 1,
            "request_id": request_id, "accepted": accepted,
            "state": state, "reason": reason, "receiver": "linux",
            "measurement_id": measurement_id,
        }
        if expected_host:
            try:
                self.udp_listener.send_json(
                    expected_host, self.config.android_port, acknowledgement,
                )
            except OSError as exc:
                self.notify(f"WARNING: IOS_CAPTURE_ONCE_ACK 发送失败：{exc}")
        self.notify(
            f"IOS_CAPTURE_ONCE_ACK request={request_id} accepted={accepted} "
            f"state={state} reason={reason}"
        )

    def _trigger(self, c1: np.ndarray, tracker: UdpMeasurementTracker) -> dict[str, Any]:
        self.measurement_id += 1
        measurement = {
            "measurement_id": self.measurement_id,
            "wall_clock_timestamp": datetime.now(timezone.utc).isoformat(),
            "playback_issue_sample": self._ring.end_sample,
            "t1_sample": None,
            "t4_sample": None,
            "tail_end_sample": None,
            "arm_sent": False,
            "arm_error": None,
            "arm_event_id": None,
            "arm_attempts": 0,
            "arm_ack": None,
            "abort_reason": None,
        }
        tracker.register(self.measurement_id)
        if self.config.capture_mode == "timed_continuous":
            # The next deadline is unknown until this round's acoustic C1 is
            # detected. Keeping the previous due sample would falsely count
            # the just-triggered cycle as an overlap.
            self.next_due_sample = None
        if self.config.android_host:
            arm_event_id = secrets.token_hex(12)
            measurement["arm_event_id"] = arm_event_id
            arm = {
                "type": "arm", "protocol_version": 1,
                "session_id": self.session_id, "measurement_id": self.measurement_id,
                "arm_event_id": arm_event_id,
            }
            self._set_state(CaptureState.WAIT_ARM_ACK)
            for attempt in range(1, self.config.udp_ack_retries + 1):
                if self.stop_event.is_set():
                    measurement["abort_reason"] = "session stopped while waiting for ARM_ACK"
                    return measurement
                measurement["arm_attempts"] = attempt
                try:
                    self.udp_listener.send_json(
                        self.config.android_host, self.config.android_port, arm,
                    )
                    measurement["arm_sent"] = True
                    self.notify(
                        f"ARM_SENT measurement={self.measurement_id} attempt="
                        f"{attempt}/{self.config.udp_ack_retries} event={arm_event_id}"
                    )
                except OSError as exc:
                    measurement["arm_error"] = str(exc)
                    self.notify(f"WARNING: ARM 第 {attempt} 次发送失败：{exc}")
                deadline = time.monotonic() + self.config.arm_ack_timeout
                while time.monotonic() < deadline and not self.stop_event.is_set():
                    self._ingest_udp(tracker)
                    ack = tracker.arm_ack_for(self.measurement_id, arm_event_id)
                    if ack is not None:
                        measurement["arm_ack"] = ack
                        if ack.get("accepted") is True:
                            break
                        measurement["abort_reason"] = (
                            f"ARM rejected by Android: {ack.get('reason', 'unknown')}"
                        )
                        return measurement
                    self._progress.wait(0.01)
                    self._progress.clear()
                if measurement["arm_ack"] is not None:
                    break
            if measurement["arm_ack"] is None:
                measurement["abort_reason"] = (
                    f"ARM_ACK timeout after {self.config.udp_ack_retries} attempts"
                )
                self.notify(f"ARM_ACK_TIMEOUT measurement={self.measurement_id}")
                return measurement
        else:
            self.notify("ARM 未发送：未配置 Android host；启用单 outstanding 兼容关联")
        measurement["playback_issue_sample"] = self._ring.end_sample
        self._set_state(CaptureState.C1_PLAYING)
        assert self.audio_backend is not None
        self.audio_backend.play(c1)
        self._set_state(CaptureState.WAIT_C2)
        return measurement

    def _live_detect(self, current: dict[str, Any], c1: np.ndarray, c2: np.ndarray) -> None:
        issue = int(current["playback_issue_sample"])
        end = self._ring.end_sample
        start, snapshot = self._ring.read(max(0, issue - round(0.02 * SAMPLE_RATE)), end)
        if snapshot.shape[0] < min(c1.size, c2.size) + 32:
            return
        statuses = channel_status(snapshot)
        issue_local = issue - start
        if current["t1_sample"] is None:
            stop = min(snapshot.shape[0], issue_local + c1.size + round(0.5 * SAMPLE_RATE))
            detection = detect_multichannel(
                snapshot, c1, self.config.c1_threshold, statuses,
                start=max(0, issue_local - round(0.02 * SAMPLE_RATE)), stop=stop,
            )
            if detection["channels_passed"] >= self.config.min_detection_channels:
                current["t1_sample"] = start + int(detection["system_sample"])
                current["live_c1_detection"] = _shift_detection(detection, start)
                self.notify(
                    f"Live C1 confirmed: sample={current['t1_sample']} | "
                    f"channels={detection['channels_passed']} | score={detection['system_score']:.3f}"
                )
                if self.config.capture_mode == "timed_continuous":
                    self.next_due_sample = current["t1_sample"] + round(self.config.interval * SAMPLE_RATE)
        if current["t1_sample"] is None:
            # A C2-like sound cannot be assigned to this round without the
            # authoritative acoustic C1 anchor. This also rejects delayed
            # repeated Android replies from an earlier manual measurement.
            return
        c2_start_global = int(current["t1_sample"]) + c1.size
        c2_start = max(0, c2_start_global - start)
        if snapshot.shape[0] >= c2_start + c2.size:
            detection = detect_multichannel(
                snapshot, c2, self.config.c2_threshold, statuses,
                start=c2_start, stop=snapshot.shape[0] - c2.size + 1,
            )
            if detection["channels_passed"] >= self.config.min_detection_channels:
                current["t4_sample"] = start + int(detection["system_sample"])
                current["live_c2_detection"] = _shift_detection(detection, start)
                self.notify(
                    f"Live C2 candidate: sample={current['t4_sample']} | "
                    f"channels={detection['channels_passed']} | score={detection['system_score']:.3f}"
                )
                current["tail_end_sample"] = (
                    current["t4_sample"] + c2.size
                    + round(max(self.config.tail, self.config.rir_duration) * SAMPLE_RATE)
                )
                self._set_state(CaptureState.CAPTURE_TAIL)

    def _finalize(
        self, current: dict[str, Any], c1: np.ndarray, c2: np.ndarray,
        tracker: UdpMeasurementTracker, *, forced_reason: str | None = None,
    ) -> dict[str, Any]:
        self._set_state(CaptureState.FINALIZE)
        issue = int(current["playback_issue_sample"])
        end = self._ring.end_sample
        start, recording = self._ring.read(max(0, issue - round(0.15 * SAMPLE_RATE)), end)
        messages = tracker.messages_for(self.measurement_id)
        analysis = analyze_recording(
            recording, c1, c2,
            playback_issue_sample=issue - start,
            c1_threshold=self.config.c1_threshold,
            c2_threshold=self.config.c2_threshold,
            reply_timeout=self.config.reply_timeout,
            android_messages=messages,
            speed_of_sound=self.config.speed_of_sound,
            linux_local_reference_correction=self.config.linux_local_reference_correction,
        )
        analysis["c1_detection"] = _shift_detection(analysis["c1_detection"], start)
        analysis["c2_detection"] = _shift_detection(analysis["c2_detection"], start)
        t1 = analysis["c1_detection"]["system_sample"]
        t4 = analysis["c2_detection"]["system_sample"]
        spatial_events: dict[str, Any] = {}
        for name, sample in (("t1", t1), ("t4", t4)):
            if sample is None:
                spatial_events[name] = {"available": False, "reason": "acoustic event was not detected"}
                continue
            timestamp_ns, clock_info = self._audio_clock.timestamp(int(sample))
            spatial: dict[str, Any] = {
                "available": False, "audio_sample": int(sample),
                "audio_time_monotonic_ns": timestamp_ns,
                "audio_clock_mapping": clock_info,
            }
            if timestamp_ns is None:
                spatial["reason"] = clock_info["reason"]
            else:
                assert self.pose_provider is not None
                radar_pose, lookup = self.pose_provider.pose_at(timestamp_ns)
                spatial["pose_lookup"] = lookup
                if radar_pose is None:
                    spatial["reason"] = lookup["reason"]
                else:
                    spatial.update({
                        "available": True,
                        "radar_pose": asdict(radar_pose),
                        "speaker_pose": transform_offset(
                            radar_pose, self.config.speaker_offset_m,
                            child_frame_id="speaker_acoustic_center",
                        ),
                        "microphone_pose": transform_offset(
                            radar_pose, self.config.microphone_offset_m,
                            child_frame_id="uma8_acoustic_center",
                        ),
                    })
            spatial_events[name] = spatial
        self._latest_spatial_events = spatial_events
        relative_t4 = None if t4 is None else int(t4) - start
        rirs, rir_info = estimate_rirs(
            recording, c2, relative_t4, analysis["channel_status"],
            method=self.config.rir_method,
            duration=self.config.rir_duration,
            regularization=self.config.deconv_lambda,
            pre_arrival=self.config.rir_pre_arrival,
        )
        quality = assess_quality(
            recording, rirs, analysis["c1_detection"], analysis["c2_detection"],
            direct_index=int(rir_info.get("direct_arrival_index", 0)),
            min_channels=self.config.min_detection_channels,
            tof_available=bool(analysis["tof"]["available"]),
            overall_policy=self.config.overall_policy,
        )
        failure_reasons = list(quality["quality_failure_reasons"])
        if forced_reason:
            failure_reasons.insert(0, forced_reason)
            quality["overall_pass"] = False
            quality["overall"] = "FAIL"
        precise = next((message for message in reversed(messages) if message.get("t3_precise") is True), None)
        result: dict[str, Any] = {
            "session_id": self.session_id,
            "measurement_id": self.measurement_id,
            "wall_clock_timestamp": current["wall_clock_timestamp"],
            "sample_rate": SAMPLE_RATE,
            "playback_issue_sample_software_only": issue,
            "t1_sample": t1,
            "t4_sample": t4,
            "android_t2": None if precise is None else precise.get("t2_sample"),
            "android_t3": None if precise is None else precise.get("t3_sample"),
            "reply_delay_samples": None if precise is None else precise.get("reply_delay_samples"),
            "reply_delay_sample_rate": None if precise is None else precise.get("sample_rate"),
            "c1_detection": analysis["c1_detection"],
            "c2_detection": analysis["c2_detection"],
            "live_c1_candidate_non_authoritative": current.get("live_c1_detection"),
            "live_c2_candidate_non_authoritative": current.get("live_c2_detection"),
            "linux_observed_roundtrip_samples": analysis["linux_observed_roundtrip_samples"],
            "linux_observed_roundtrip_ms": analysis["linux_observed_roundtrip_ms"],
            "tof": analysis["tof"],
            "exact_tof": analysis["tof"].get("tof_seconds", "NOT AVAILABLE"),
            "tof_available": bool(analysis["tof"]["available"]),
            "android": {
                "messages": messages,
                "association": tracker.association_for(self.measurement_id),
                "t3_precise": precise is not None,
                "arm_sent": current["arm_sent"],
                "arm_error": current["arm_error"],
                "arm_event_id": current.get("arm_event_id"),
                "arm_attempts": current.get("arm_attempts", 0),
                "arm_ack": current.get("arm_ack"),
            },
            "rir": rir_info,
            "quality": quality,
            "failure_reasons": failure_reasons,
            "capture": {"window_start_sample": start, "window_end_sample": end, "frames": recording.shape[0]},
            "tx_pose": spatial_events["t1"].get("speaker_pose"),
            "rx_pose": spatial_events["t4"].get("microphone_pose"),
            "local_spatial_events": spatial_events,
            "pose_extrinsics": {
                "speaker_offset_m": list(self.config.speaker_offset_m),
                "microphone_offset_m": list(self.config.microphone_offset_m),
            },
        }
        assert self._writer is not None
        measurement_dir = self._writer.write_measurement(self.measurement_id, result, rirs)
        try:
            write_all_plots(measurement_dir / "plots", recording, c1, c2, {
                **result,
                "c1_detection": {
                    **result["c1_detection"],
                    "system_sample": None if t1 is None else t1 - start,
                },
                "c2_detection": {
                    **result["c2_detection"],
                    "system_sample": None if t4 is None else t4 - start,
                },
            }, rirs)
        except Exception as exc:
            self.notify(f"WARNING: measurement {self.measurement_id} 绘图失败：{exc}")
        if self.rir_preview is not None:
            self.rir_preview(rirs, True)
        tracker.complete(self.measurement_id)
        self._latest_quality = quality
        if quality["overall_pass"]:
            self.success_count += 1
        else:
            self.failure_count += 1
        self.notify(
            f"measurement {self.measurement_id:06d}: {quality['overall']} | "
            f"ToF={'available' if analysis['tof']['available'] else 'NOT AVAILABLE'}"
        )
        return result

    def run(self) -> tuple[Path, dict[str, Any]]:
        cfg = self.config
        cfg.validate()
        if cfg.capture_mode not in {"manual_continuous", "timed_continuous"}:
            raise ValueError("ContinuousController 仅用于持续采集模式")
        c1, c1_warnings = load_probe(cfg.c1, warning=self.notify)
        c2, c2_warnings = load_probe(cfg.c2, warning=self.notify)
        input_info = resolve_device_info(cfg.input_device, input_device=True)
        output_info = resolve_device_info(cfg.output_device, input_device=False)
        if self.audio_backend is None:
            self.audio_backend = ContinuousAudioBackend(
                input_info, output_info, cfg.output_channel, cfg.playback_gain
            )
        self._writer = SessionWriter(cfg.output_root, self.session_id)
        self.session_id = self._writer.session_id
        for existing in self._log_lines:
            self._writer.append_log(existing)
        self._log_lines.clear()
        self.udp_listener = self.udp_listener or UdpListener(cfg.udp_host, cfg.udp_port, self.notify)
        if self.pose_provider is None:
            if cfg.pose_source == "udp":
                self.pose_provider = UdpPoseProvider(
                    cfg.pose_udp_host, cfg.pose_udp_port, cfg.pose_max_age,
                )
            elif cfg.pose_source == "manual":
                self.pose_provider = ManualPoseProvider(cfg.manual_position_m)
            else:
                self.pose_provider = NullPoseProvider()
        tracker = UdpMeasurementTracker(self.session_id, compatibility_mode=cfg.udp_compatibility_mode)
        capacity_s = max(30.0, cfg.reply_timeout + cfg.tail + cfg.rir_duration + 2.0)
        self._ring = PcmRingBuffer(round(capacity_s * SAMPLE_RATE))
        self._audio_clock = AudioSampleClock(SAMPLE_RATE)
        start_timestamp = datetime.now(timezone.utc).isoformat()
        summary: dict[str, Any] = {
            "session_id": self.session_id,
            "start_timestamp": start_timestamp,
            "end_timestamp": None,
            "capture_mode": cfg.capture_mode,
            "interval": cfg.interval,
            "sample_rate": SAMPLE_RATE,
            "input_device": input_info.to_dict(),
            "output_device": output_info.to_dict(),
            "output_channel": cfg.output_channel,
            "playback_gain": cfg.playback_gain,
            "c1": {**wav_metadata(cfg.c1), "sha256": _sha256(cfg.c1)},
            "c2": {**wav_metadata(cfg.c2), "sha256": _sha256(cfg.c2)},
            "output_directory": str(self._writer.directory.resolve()),
            "success_count": 0, "failure_count": 0, "skipped_count": 0,
            "interrupted": True,
            "probe_warnings": c1_warnings + c2_warnings,
            "pose_extrinsics": {
                "speaker_offset_m": list(cfg.speaker_offset_m),
                "microphone_offset_m": list(cfg.microphone_offset_m),
            },
        }
        self._writer.update_session(summary)
        for warning in output_warnings(output_info):
            self.notify(warning)
        current: dict[str, Any] | None = None
        interval_frames = round(cfg.interval * SAMPLE_RATE)
        session_started_monotonic = time.monotonic()
        termination_reason = "completed"
        state_before_stop = self.state.value

        def accept(block: np.ndarray, warning: str | None = None) -> None:
            if warning:
                self.notify(f"WARNING: input stream: {warning}")
            values = np.ascontiguousarray(block, dtype=np.float32)
            _start, end = self._ring.append(values)
            self._audio_clock.add_anchor(end)
            assert self._writer is not None
            self._writer.raw.write(values)
            if self.audio_block:
                self.audio_block(values)
            self._progress.set()

        self.pose_provider.start()
        self.udp_listener.start()
        try:
            with self.audio_backend.capture(accept) as input_session:
                self._set_state(CaptureState.ARMED)
                if cfg.capture_mode == "timed_continuous":
                    self.next_due_sample = self._ring.end_sample + round(cfg.startup_countdown * SAMPLE_RATE)
                    self.notify(f"自动采集将在 {cfg.startup_countdown:.1f} 秒倒计时后开始")
                while True:
                    self._ingest_udp(tracker)
                    end = self._ring.end_sample
                    if getattr(input_session, "finished", threading.Event()).is_set():
                        errors = getattr(input_session, "errors", [])
                        raise RuntimeError(errors[-1] if errors else "持续输入流意外结束")
                    if (
                        cfg.max_session_duration
                        and time.monotonic() - session_started_monotonic >= cfg.max_session_duration
                    ):
                        termination_reason = "max_session_duration"
                        self.stop_event.set()
                    if cfg.max_measurements and self.measurement_id >= cfg.max_measurements and current is None:
                        termination_reason = "max_measurements"
                        self.stop_event.set()
                    if self.stop_event.is_set():
                        if termination_reason == "completed":
                            termination_reason = "safe_stop"
                        state_before_stop = self.state.value
                        if current is not None:
                            self._finalize(current, c1, c2, tracker, forced_reason="session_stopped_during_measurement")
                            current = None
                        break

                    if (
                        cfg.capture_mode == "timed_continuous"
                        and current is not None and self.next_due_sample is not None
                        and end >= self.next_due_sample
                    ):
                        while end >= self.next_due_sample:
                            self.skip_count += 1
                            self.skip_events.append({
                                "sample": self.next_due_sample,
                                "reason": "automatic_cycle_due_while_busy",
                                "state": self.state.value,
                            })
                            self.notify(
                                f"自动周期跳过：sample {self.next_due_sample} 到期时状态为 {self.state.value}"
                            )
                            self.next_due_sample += interval_frames

                    if current is None and self.state == CaptureState.ARMED and not self.pause_event.is_set():
                        trigger = False
                        with self._command_lock:
                            if self._manual_requests:
                                self._manual_requests -= 1
                                trigger = True
                        if not trigger and self.next_due_sample is not None and end >= self.next_due_sample:
                            trigger = True
                        if trigger:
                            current = self._trigger(c1, tracker)
                            if current.get("abort_reason"):
                                self._finalize(
                                    current, c1, c2, tracker,
                                    forced_reason=str(current["abort_reason"]),
                                )
                                current = None
                                if cfg.capture_mode == "timed_continuous":
                                    self.next_due_sample = self._ring.end_sample + interval_frames
                                self._set_state(CaptureState.ARMED)

                    if current is not None and self.state == CaptureState.WAIT_C2:
                        self._live_detect(current, c1, c2)
                        timeout_sample = (
                            int(current["playback_issue_sample"]) + c1.size
                            + round(cfg.reply_timeout * SAMPLE_RATE)
                        )
                        if self.state == CaptureState.WAIT_C2 and end >= timeout_sample:
                            if cfg.capture_mode == "timed_continuous" and self.next_due_sample is None:
                                self.skip_count += 1
                                self.skip_events.append({
                                    "sample": end,
                                    "reason": "C1_acoustic_anchor_missing_schedule_deferred",
                                    "state": self.state.value,
                                })
                                self.next_due_sample = end + interval_frames
                            self._finalize(current, c1, c2, tracker, forced_reason="C2 timeout")
                            current = None
                            self._set_state(CaptureState.ARMED)

                    if (
                        current is not None and self.state == CaptureState.CAPTURE_TAIL
                        and end >= int(current["tail_end_sample"])
                    ):
                        self._finalize(current, c1, c2, tracker)
                        current = None
                        self._set_state(CaptureState.ARMED)

                    self._emit_status()
                    self._progress.wait(0.02)
                    self._progress.clear()
        except KeyboardInterrupt:
            self.stop_event.set()
            termination_reason = "keyboard_interrupt"
            state_before_stop = self.state.value
            self.notify("Ctrl+C：正在安全停止并保存")
            if current is not None:
                self._finalize(current, c1, c2, tracker, forced_reason="keyboard_interrupt")
                current = None
        except Exception:
            termination_reason = "error"
            state_before_stop = self.state.value
            raise
        finally:
            # Drain datagrams already delivered before the socket closes.
            self._ingest_udp(tracker)
            self.udp_listener.stop()
            self._ingest_udp(tracker)
            self.pose_provider.stop()
            self._set_state(CaptureState.IDLE)
            summary.update({
                "end_timestamp": datetime.now(timezone.utc).isoformat(),
                "success_count": self.success_count,
                "failure_count": self.failure_count,
                "skipped_count": self.skip_count,
                "skipped_events": self.skip_events,
                "interrupted": termination_reason in {"keyboard_interrupt", "error"},
                "termination_reason": termination_reason,
                "last_state_before_stop": state_before_stop,
                "udp_listener_error": self.udp_listener.error,
                "pose_interface": self.pose_provider.metadata(),
            })
            self._writer.update_session(summary)
            self._writer.close()
        return self._writer.directory, summary
