from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time

import numpy as np
import pytest
from scipy import signal
from scipy.io import wavfile

import avtwin_linux.role_session as role_session
from avtwin_linux.audio_io import AudioDeviceInfo
from avtwin_linux.batch_stats import summarize
from avtwin_linux.config import CHANNELS, SAMPLE_RATE, ControllerConfig
from avtwin_linux.role_session import HandshakeSession
from avtwin_linux.pose import PoseSample


def _probe(path: Path, low: float, high: float) -> np.ndarray:
    count = 480
    timeline = np.arange(count) / SAMPLE_RATE
    values = (0.7 * signal.chirp(timeline, f0=low, f1=high, t1=count / SAMPLE_RATE)
              * signal.windows.tukey(count, 0.2)).astype(np.float32)
    wavfile.write(path, SAMPLE_RATE, np.rint(values * 32767).astype(np.int16))
    return values


def _device(input_device: bool) -> AudioDeviceInfo:
    return AudioDeviceInfo(
        display_name="fake input" if input_device else "fake output",
        backend="test", portaudio_index=0 if input_device else 1, hostapi="test",
        max_input_channels=CHANNELS if input_device else 0,
        max_output_channels=0 if input_device else 2, default_samplerate=SAMPLE_RATE,
        alsa_card=None, alsa_card_id=None, alsa_device=None, alsa_hw=None,
        alsa_stable_hw=None, stable_name="fake:in" if input_device else "fake:out",
        is_input_candidate=input_device, is_output_candidate=not input_device,
        is_analog_output=not input_device, is_digital_output=False,
        is_virtual=False, is_uma8=input_device, alsa_has_capture=False, alsa_has_playback=False,
    )


class FakeUdp:
    def __init__(self, order: list[str]):
        self.order = order
        self.messages: list[dict] = []
        self.sent_messages: list[dict] = []
        self.sent_ports: list[int] = []
        self.error = None

    def start(self): pass
    def stop(self): pass

    def send_json(self, _host: str, _port: int, message: dict) -> None:
        self.order.append("udp")
        self.sent_messages.append(message)
        self.sent_ports.append(_port)
        if message.get("type") == "arm":
            self.messages.append({
                "type": "arm_ack", "protocol_version": 1,
                "session_id": message["session_id"],
                "measurement_id": message["measurement_id"],
                "arm_event_id": message["arm_event_id"],
                "accepted": True, "reason": "accepted_strict",
            })


class FakeAudio:
    def __init__(self, c1: np.ndarray, c2: np.ndarray, role: str, order: list[str]):
        self.c1, self.c2, self.role, self.order = c1, c2, role, order
        self.cursor = 0
        self.events: list[tuple[int, np.ndarray, float]] = []
        self.stop_event = threading.Event()
        self.finished = threading.Event()
        self.errors: list[str] = []
        if role == "responder":
            self.events.append((1500, c1, 0.9))

    @contextmanager
    def capture(self, callback):
        def generate() -> None:
            rng = np.random.default_rng(78)
            while not self.stop_event.is_set():
                start, stop = self.cursor, self.cursor + 128
                self.cursor = stop
                block = rng.normal(0, 0.0002, (128, CHANNELS)).astype(np.float32)
                block[:, 7] = 0.0
                for event_start, waveform, gain in list(self.events):
                    left, right = max(start, event_start), min(stop, event_start + waveform.size)
                    if right > left:
                        block[left - start:right - start, :7] += gain * waveform[left - event_start:right - event_start, None]
                callback(block, None)
                time.sleep(0.0005)
            self.finished.set()

        thread = threading.Thread(target=generate, daemon=True)
        thread.start()
        try:
            yield self
        finally:
            self.stop_event.set()
            thread.join(timeout=1)

    def play(self, probe: np.ndarray) -> None:
        self.order.append("play")
        if self.role == "initiator":
            acoustic = self.cursor + 128
            self.events.append((acoustic, self.c1, 0.9))
            self.events.append((acoustic + self.c1.size + 700, self.c2, 0.8))
        else:
            self.events.append((self.cursor + 128, self.c2, 0.9))


class ConstantPoseProvider:
    def __init__(self):
        half = np.sqrt(0.5)
        self.pose = PoseSample(
            timestamp_ns=1, position_m=(10.0, 20.0, 1.0),
            orientation_xyzw=(0.0, 0.0, float(half), float(half)),
            source="test",
        )

    def start(self): pass
    def stop(self): pass
    def latest(self): return self.pose
    def pose_at(self, timestamp_ns):
        return PoseSample(
            timestamp_ns=timestamp_ns, position_m=self.pose.position_m,
            orientation_xyzw=self.pose.orientation_xyzw, source="test",
        ), {"available": True, "method": "constant", "nearest_age_ms": 0.0}
    def metadata(self): return {"source": "test", "received": 1, "rejected": 0}


def _run(monkeypatch, tmp_path: Path, role: str, pose_provider=None):
    c1_path, c2_path = tmp_path / "c1.wav", tmp_path / "c2.wav"
    c1, c2 = _probe(c1_path, 9000, 14000), _probe(c2_path, 800, 5000)
    cfg = ControllerConfig(
        c1=c1_path, c2=c2_path, role=role, input_device="fake:in", output_device="fake:out",
        output_root=tmp_path / "output", pre_roll=0.01, reply_timeout=0.8,
        tail=0.01, rir_duration=0.03, rir_pre_arrival=0.005,
        c1_threshold=0.5, c2_threshold=0.5, overall_policy="protocol",
        android_host="127.0.0.1",
        android_port=7001,
        speaker_offset_m=(1.0, 0.0, 0.0),
        microphone_offset_m=(0.0, 1.0, 0.0),
    )
    order: list[str] = []
    backend, udp = FakeAudio(c1, c2, role, order), FakeUdp(order)
    monkeypatch.setattr(role_session, "resolve_device_info", lambda _value, *, input_device: _device(input_device))
    monkeypatch.setattr(role_session, "_save_correlations", lambda *_args: None)
    directory, result = HandshakeSession(
        cfg, audio_backend=backend, udp_listener=udp, pose_provider=pose_provider,
    ).run()
    return order, directory, result, udp


def test_initiator_single_session(monkeypatch, tmp_path) -> None:
    _order, directory, result, udp = _run(monkeypatch, tmp_path, "initiator")
    assert result["result"] == "SUCCESS"
    assert result["t1"] is not None and result["t4"] > result["t1"]
    assert result["inactive_channels"] == [7]
    assert result["arm_handshake"]["accepted"] is True
    assert any(item["type"] == "arm" for item in udp.sent_messages)
    assert (directory / "rir" / "remote" / "rir_fused.wav").is_file()
    assert (directory / "rir" / "local" / "rir_ch0.wav").is_file()
    assert udp.sent_messages[-1]["type"] == "initiator_timing"
    assert udp.sent_messages[-1]["timing_precise"] is True
    assert set(udp.sent_ports) == {7001}


def test_responder_plays_before_network_and_saves_precise_times(monkeypatch, tmp_path) -> None:
    order, directory, result, udp = _run(monkeypatch, tmp_path, "responder")
    assert order.index("play") < order.index("udp")
    assert result["result"] == "SUCCESS"
    assert result["t2_realtime_sample"] is not None
    assert result["t2"] is not None and result["t3"] > result["t2"]
    assert result["turnaround_samples"] > 0
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["role"] == "RESPONDER"
    assert metadata["timestamp_basis"] == "audio_sample_index"
    assert metadata["inactive_channels"] == [7]
    assert udp.sent_messages[-1]["type"] == "reply_timing"
    assert udp.sent_messages[-1]["t3_precise"] is True
    assert set(udp.sent_ports) == {7001}


def test_batch_summary_groups_roles(tmp_path) -> None:
    session = tmp_path / "20260101_000000_responder"
    session.mkdir()
    (session / "metadata.json").write_text(json.dumps({
        "protocol": "AVTWIN_V1", "role": "RESPONDER", "sample_rate": SAMPLE_RATE,
        "c1_scores": {"channels_passed": 7}, "c2_scores": {"channels_passed": 7},
        "realtime_detection": {"c1": {"system_sample": 100}},
        "turnaround_samples": 480, "remote_rir": {"available": True},
        "tof": {"available": False},
    }))
    rows = summarize(tmp_path)
    assert rows[0]["role"] == "RESPONDER"
    assert rows[0]["runs"] == 1
    assert rows[0]["turnaround_mean_ms"] == 10.0
    assert rows[0]["rir_extraction_success_rate"] == 1.0


def test_handshake_saves_speaker_and_microphone_world_positions(monkeypatch, tmp_path) -> None:
    _order, _directory, result, _udp = _run(
        monkeypatch, tmp_path, "initiator", ConstantPoseProvider(),
    )
    event = result["local_spatial_events"]["t4"]
    assert event["available"] is True
    assert event["speaker_pose"]["position_m"] == pytest.approx((10.0, 21.0, 1.0))
    assert event["microphone_pose"]["position_m"] == pytest.approx((9.0, 20.0, 1.0))
