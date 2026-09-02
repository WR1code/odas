from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time

import numpy as np
from scipy import signal
from scipy.io import wavfile

import avtwin_linux.continuous as continuous
import avtwin_linux.controller as single_controller
from avtwin_linux.audio_io import AudioDeviceInfo, CaptureResult
from avtwin_linux.config import CHANNELS, SAMPLE_RATE, ControllerConfig
from avtwin_linux.output_paths import validate_output_root
from avtwin_linux.result_writer import SessionWriter, repair_float32_wav_header
from avtwin_linux.udp_listener import UdpMeasurementTracker
from avtwin_linux.wav_utils import write_float32


def make_probe(path: Path, f0: float, f1: float, duration: float = 0.008) -> np.ndarray:
    count = round(duration * SAMPLE_RATE)
    timeline = np.arange(count) / SAMPLE_RATE
    probe = (0.6 * signal.chirp(timeline, f0=f0, f1=f1, t1=duration)
             * signal.windows.tukey(count, 0.25)).astype(np.float32)
    wavfile.write(path, SAMPLE_RATE, np.rint(probe * 32767).astype(np.int16))
    return probe


def fake_device(input_device: bool) -> AudioDeviceInfo:
    return AudioDeviceInfo(
        display_name="fake input" if input_device else "fake output",
        backend="simulated", portaudio_index=0 if input_device else 1,
        hostapi="test", max_input_channels=CHANNELS if input_device else 0,
        max_output_channels=0 if input_device else 2, default_samplerate=SAMPLE_RATE,
        alsa_card=None, alsa_card_id=None, alsa_device=None, alsa_hw=None,
        alsa_stable_hw=None, stable_name="fake:input" if input_device else "fake:output",
        is_input_candidate=input_device, is_output_candidate=not input_device,
        is_analog_output=not input_device, is_digital_output=False,
        is_virtual=False, is_uma8=input_device, alsa_has_capture=False,
        alsa_has_playback=False,
    )


class SimulatedAudio:
    def __init__(self, c1: np.ndarray, c2: np.ndarray, *, reply: bool = True, block: int = 128):
        self.c1 = c1
        self.c2 = c2
        self.reply = reply
        self.block = block
        self.cursor = 0
        self.callback = None
        self.events: list[tuple[int, np.ndarray, float]] = []
        self.play_samples: list[int] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.finished = threading.Event()
        self.errors: list[str] = []

    @contextmanager
    def capture(self, callback):
        self.callback = callback

        def generate() -> None:
            rng = np.random.default_rng(451)
            while not self._stop.is_set():
                with self._lock:
                    start = self.cursor
                    stop = start + self.block
                    self.cursor = stop
                    events = list(self.events)
                values = rng.normal(0, 0.0005, (self.block, CHANNELS)).astype(np.float32)
                for event_start, waveform, gain in events:
                    event_stop = event_start + waveform.size
                    left, right = max(start, event_start), min(stop, event_stop)
                    if right > left:
                        values[left - start:right - start] += gain * waveform[left - event_start:right - event_start, None]
                callback(values, None)
                time.sleep(0.001)
            self.finished.set()

        thread = threading.Thread(target=generate, daemon=True)
        thread.start()
        try:
            yield self
        finally:
            self._stop.set()
            thread.join(timeout=1)

    def play(self, probe: np.ndarray) -> None:
        with self._lock:
            acoustic_c1 = self.cursor + 128
            self.play_samples.append(acoustic_c1)
            self.events.append((acoustic_c1, self.c1, 0.9))
            if self.reply:
                self.events.append((acoustic_c1 + self.c1.size + 900, self.c2, 0.75))


class AckingUdp:
    def __init__(self, *, accepted: bool = True):
        self.accepted = accepted
        self.messages: list[dict] = []
        self.raw_lines: list[str] = []
        self.sent_messages: list[dict] = []
        self.error = None

    def start(self): pass
    def stop(self): pass

    def send_json(self, _host: str, _port: int, message: dict) -> None:
        self.sent_messages.append(message)
        if message.get("type") == "arm" and not any(
            item.get("arm_event_id") == message["arm_event_id"] for item in self.messages
        ):
            ack = {
                "type": "arm_ack", "protocol_version": 1,
                "session_id": message["session_id"],
                "measurement_id": message["measurement_id"],
                "arm_event_id": message["arm_event_id"],
                "accepted": self.accepted,
                "reason": "accepted_strict" if self.accepted else "source_rejected",
            }
            self.messages.append(ack)
            self.raw_lines.append(json.dumps(ack))


def config(tmp_path: Path, mode: str, *, max_measurements: int = 3, interval: float = 0.08):
    c1_path, c2_path = tmp_path / "c1.wav", tmp_path / "c2.wav"
    c1 = make_probe(c1_path, 9_000, 14_000)
    c2 = make_probe(c2_path, 800, 5_000)
    cfg = ControllerConfig(
        c1=c1_path, c2=c2_path, input_device="fake:input", output_device="fake:output",
        capture_mode=mode, interval=interval, max_measurements=max_measurements,
        startup_countdown=0, reply_timeout=0.08, tail=0.006, rir_duration=0.06,
        rir_pre_arrival=0.005, c1_threshold=0.50, c2_threshold=0.50,
        output_root=tmp_path / "results", overall_policy="protocol",
    )
    return cfg, c1, c2


def patch_hardware(monkeypatch) -> None:
    monkeypatch.setattr(
        continuous, "resolve_device_info",
        lambda _value, *, input_device: fake_device(input_device),
    )
    monkeypatch.setattr(continuous, "write_all_plots", lambda *_args, **_kwargs: None)


def wait_until(predicate, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_manual_continuous_triggers_three_rounds(monkeypatch, tmp_path) -> None:
    patch_hardware(monkeypatch)
    cfg, c1, c2 = config(tmp_path, "manual_continuous")
    backend = SimulatedAudio(c1, c2)
    controller = continuous.ContinuousController(cfg, audio_backend=backend)
    outcome: list[tuple[Path, dict]] = []
    thread = threading.Thread(target=lambda: outcome.append(controller.run()), daemon=True)
    thread.start()
    for expected in range(3):
        wait_until(lambda: controller.state == continuous.CaptureState.ARMED and controller.measurement_id == expected)
        assert controller.request_capture()
        wait_until(lambda: controller.measurement_id > expected)
    thread.join(timeout=8)
    assert not thread.is_alive()
    directory, summary = outcome[0]
    assert summary["success_count"] == 3
    assert len(list((directory / "measurements").glob("*/result.json"))) == 3
    assert backend.play_samples == sorted(backend.play_samples)


def test_timed_mode_never_overlaps_and_records_busy_skips(monkeypatch, tmp_path) -> None:
    patch_hardware(monkeypatch)
    cfg, c1, c2 = config(tmp_path, "timed_continuous", interval=0.005)
    backend = SimulatedAudio(c1, c2)
    controller = continuous.ContinuousController(cfg, audio_backend=backend)
    _directory, summary = controller.run()
    assert len(backend.play_samples) == 3
    assert summary["skipped_count"] > 0
    assert all(right > left for left, right in zip(backend.play_samples, backend.play_samples[1:]))


def test_ios_button_can_insert_capture_in_timed_mode(monkeypatch, tmp_path) -> None:
    patch_hardware(monkeypatch)
    cfg, c1, c2 = config(tmp_path, "timed_continuous", max_measurements=1)
    cfg.startup_countdown = 5.0
    cfg.android_host = "192.0.2.10"
    udp = AckingUdp(accepted=True)
    udp.messages.append({
        "type": "capture_once_request", "protocol_version": 1,
        "request_id": "ios-request-1", "ios_control_port": cfg.android_port,
        "source": "192.0.2.10:49152",
    })
    controller = continuous.ContinuousController(
        cfg, audio_backend=SimulatedAudio(c1, c2), udp_listener=udp,
    )

    _directory, summary = controller.run()

    assert summary["success_count"] == 1
    acknowledgements = [
        message for message in udp.sent_messages
        if message.get("type") == "capture_once_ack"
    ]
    assert acknowledgements == [{
        "type": "capture_once_ack", "protocol_version": 1,
        "request_id": "ios-request-1", "accepted": True,
        "state": "ARMED", "reason": "accepted_queued", "receiver": "linux",
        "measurement_id": None,
    }]


def test_c2_timeout_returns_to_armed_for_next_round(monkeypatch, tmp_path) -> None:
    patch_hardware(monkeypatch)
    cfg, c1, c2 = config(tmp_path, "timed_continuous", max_measurements=2, interval=0.12)
    backend = SimulatedAudio(c1, c2, reply=False)
    controller = continuous.ContinuousController(cfg, audio_backend=backend)
    directory, summary = controller.run()
    assert summary["failure_count"] == 2
    reasons = [json.loads(path.read_text())["failure_reasons"] for path in sorted(directory.glob("measurements/*/result.json"))]
    assert all("C2 timeout" in item for item in reasons)


def test_c1_is_played_only_after_correlated_arm_ack(monkeypatch, tmp_path) -> None:
    patch_hardware(monkeypatch)
    cfg, c1, c2 = config(tmp_path, "timed_continuous", max_measurements=1)
    cfg.android_host = "192.0.2.10"
    backend = SimulatedAudio(c1, c2)
    udp = AckingUdp(accepted=True)
    directory, _summary = continuous.ContinuousController(
        cfg, audio_backend=backend, udp_listener=udp,
    ).run()
    result = json.loads(next(directory.glob("measurements/*/result.json")).read_text())
    assert len(backend.play_samples) == 1
    assert result["android"]["arm_ack"]["accepted"] is True
    assert result["android"]["arm_attempts"] == 1


def test_rejected_arm_fails_early_without_playing_c1(monkeypatch, tmp_path) -> None:
    patch_hardware(monkeypatch)
    cfg, c1, c2 = config(tmp_path, "timed_continuous", max_measurements=1)
    cfg.android_host = "192.0.2.10"
    backend = SimulatedAudio(c1, c2)
    udp = AckingUdp(accepted=False)
    directory, summary = continuous.ContinuousController(
        cfg, audio_backend=backend, udp_listener=udp,
    ).run()
    result = json.loads(next(directory.glob("measurements/*/result.json")).read_text())
    assert backend.play_samples == []
    assert summary["failure_count"] == 1
    assert "ARM rejected by Android: source_rejected" in result["failure_reasons"]


def test_pause_resume_and_safe_stop(monkeypatch, tmp_path) -> None:
    patch_hardware(monkeypatch)
    cfg, c1, c2 = config(tmp_path, "timed_continuous", max_measurements=0, interval=0.08)
    cfg.startup_countdown = 0.05
    backend = SimulatedAudio(c1, c2)
    controller = continuous.ContinuousController(cfg, audio_backend=backend)
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(controller.run()), daemon=True)
    thread.start()
    wait_until(lambda: controller.state == continuous.CaptureState.ARMED)
    controller.pause()
    time.sleep(0.08)
    assert not backend.play_samples
    controller.resume()
    wait_until(lambda: controller.measurement_id >= 1)
    wait_until(lambda: controller.state == continuous.CaptureState.ARMED)
    controller.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    directory, _summary = outcome[0]
    assert (directory / "session.json").is_file()
    assert (directory / "raw" / "continuous_float32.wav").stat().st_size > 44


def test_maximum_wall_clock_session_duration(monkeypatch, tmp_path) -> None:
    patch_hardware(monkeypatch)
    cfg, c1, c2 = config(tmp_path, "timed_continuous", max_measurements=0)
    cfg.startup_countdown = 5.0
    cfg.max_session_duration = 0.05
    controller = continuous.ContinuousController(cfg, audio_backend=SimulatedAudio(c1, c2))
    started = time.monotonic()
    _directory, summary = controller.run()
    assert time.monotonic() - started < 1.0
    assert summary["termination_reason"] == "max_session_duration"
    assert summary["success_count"] == summary["failure_count"] == 0


def test_udp_duplicate_late_out_of_order_and_mismatch() -> None:
    tracker = UdpMeasurementTracker("session-A")
    tracker.register(1)
    valid = {"type": "reply_timing", "protocol_version": 1, "session_id": "session-A",
             "measurement_id": 1, "t3_precise": True, "reply_delay_samples": 42}
    assert tracker.ingest(valid)["status"] == "accepted"
    assert tracker.ingest(dict(valid))["status"] == "duplicate"
    assert tracker.ingest({**valid, "measurement_id": 2})["status"] == "measurement_id_mismatch"
    assert tracker.ingest({**valid, "session_id": "session-B", "measurement_id": 1})["status"] == "session_mismatch"
    tracker.complete(1)
    late = {**valid, "reply_delay_samples": 43}
    assert tracker.ingest(late)["status"] == "late"
    tracker.register(2)
    assert tracker.messages_for(2) == []


def test_arm_ack_is_correlated_by_session_measurement_and_event() -> None:
    tracker = UdpMeasurementTracker("session-A")
    tracker.register(7)
    ack = {
        "type": "arm_ack", "protocol_version": 1,
        "session_id": "session-A", "measurement_id": 7,
        "arm_event_id": "arm-event-7", "accepted": True,
        "reason": "accepted_strict",
    }
    event = tracker.ingest(ack)
    assert event["status"] == "arm_ack_accepted"
    assert tracker.arm_ack_for(7, "wrong-event") is None
    assert tracker.arm_ack_for(7, "arm-event-7") == ack


def test_reply_timing_is_acknowledged_to_android_control_port(tmp_path) -> None:
    cfg, _c1, _c2 = config(tmp_path, "manual_continuous")
    cfg.android_host = "192.0.2.10"
    cfg.android_port = 7001
    udp = AckingUdp()
    udp.messages.append({
        "type": "reply_timing", "protocol_version": 1,
        "session_id": "session-A", "measurement_id": 3,
        "android_event_id": "reply-event-3", "t3_precise": False,
    })
    tracker = UdpMeasurementTracker("session-A")
    tracker.register(3)
    controller = continuous.ContinuousController(cfg, udp_listener=udp, session_id="session-A")
    controller._ingest_udp(tracker)
    ack = udp.sent_messages[-1]
    assert ack["type"] == "reply_ack"
    assert ack["android_event_id"] == "reply-event-3"
    assert ack["accepted"] is True


def test_float32_rir_tail_survives_roundtrip(tmp_path) -> None:
    path = tmp_path / "tiny.wav"
    values = np.array([1.0, 2e-8, -3e-9], dtype=np.float32)
    write_float32(path, values)
    rate, loaded = wavfile.read(path)
    assert rate == SAMPLE_RATE
    assert loaded.dtype == np.float32
    assert loaded[1] != 0 and loaded[2] != 0
    assert np.array_equal(values, loaded)


def test_output_directory_validation_and_unwritable_error(tmp_path) -> None:
    selected = validate_output_root(tmp_path / "chosen" / "nested")
    assert selected.is_absolute() and selected.is_dir()
    selected.chmod(0o555)
    try:
        try:
            validate_output_root(selected)
        except ValueError as exc:
            assert "不可创建或不可写" in str(exc)
        else:
            raise AssertionError("unwritable directory was accepted")
    finally:
        selected.chmod(0o755)


def test_interrupted_session_summary_remains_recoverable(tmp_path) -> None:
    writer = SessionWriter(tmp_path, "recovery")
    writer.update_session({"session_id": "recovery", "interrupted": True, "success_count": 1})
    writer.raw.write(np.full((16, CHANNELS), 1e-7, dtype=np.float32))
    writer.close()
    recovered = json.loads((writer.directory / "session.json").read_text())
    assert recovered["session_id"] == "recovery"
    assert recovered["interrupted"] is True
    assert recovered["end_timestamp"]


def test_interrupted_float32_wav_header_can_be_repaired(tmp_path) -> None:
    writer = SessionWriter(tmp_path, "wav-recovery")
    values = np.full((123, CHANNELS), 2e-8, dtype=np.float32)
    writer.raw.write(values)
    path = writer.raw.path
    writer.raw._file.flush()
    # Simulate termination before close: the initial header still says zero.
    writer.raw._file.close()
    frames = repair_float32_wav_header(path)
    rate, recovered = wavfile.read(path)
    assert frames == 123 and rate == SAMPLE_RATE
    assert recovered.shape == values.shape
    assert np.array_equal(recovered, values)


def test_single_capture_mode_is_default_and_compatible(monkeypatch, tmp_path) -> None:
    c1 = tmp_path / "c1.wav"
    c2 = tmp_path / "c2.wav"
    make_probe(c1, 8_000, 12_000)
    make_probe(c2, 800, 4_000)
    cfg = ControllerConfig(c1=c1, c2=c2, output_root=tmp_path / "output")
    cfg.validate()
    assert cfg.capture_mode == "single"
    input_info, output_info = fake_device(True), fake_device(False)
    monkeypatch.setattr(
        single_controller, "resolve_device_info",
        lambda _value, *, input_device: input_info if input_device else output_info,
    )
    monkeypatch.setattr(single_controller, "check_audio_configuration", lambda *_args: (8, 2))
    monkeypatch.setattr(single_controller, "write_all_plots", lambda *_args, **_kwargs: None)

    def capture(c1_probe, c2_probe, **_kwargs):
        recording = np.random.default_rng(11).normal(0, 0.0005, (32_000, CHANNELS)).astype(np.float32)
        t1, t4 = 2_000, 7_000
        recording[t1:t1 + c1_probe.size] += 0.8 * c1_probe[:, None]
        recording[t4:t4 + c2_probe.size] += 0.7 * c2_probe[:, None]
        return CaptureResult(recording, t1 - 64, t4, [], False)

    class NoUdp:
        def __init__(self, *_args, **_kwargs):
            self.messages, self.raw_lines, self.error = [], [], None

        def start(self): pass
        def stop(self): pass

    monkeypatch.setattr(single_controller, "capture_handshake", capture)
    monkeypatch.setattr(single_controller, "UdpListener", NoUdp)
    directory, result = single_controller.Controller(cfg).run()
    assert result["measurement_id"] == 1
    assert result["exact_tof"] == "NOT AVAILABLE"
    assert (directory / "raw_linux_8ch.wav").is_file()
    assert (directory / "rir" / "rir_ch0.wav").is_file()
