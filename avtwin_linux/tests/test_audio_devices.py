from __future__ import annotations

import numpy as np
import threading
import time

import avtwin_linux.audio_io as audio_io


def alsa_entry(card: int, device: int, card_id: str, name: str, directions: set[str]):
    return {
        "card": card,
        "device": device,
        "card_id": card_id,
        "card_name": name,
        "device_name": name,
        "device_label": name,
        "directions": directions,
    }


def pa_item(name: str, inputs: int, outputs: int, rate: float = 44_100.0):
    return {
        "name": name,
        "hostapi": 0,
        "max_input_channels": inputs,
        "max_output_channels": outputs,
        "default_samplerate": rate,
    }


def sample_devices() -> list[audio_io.AudioDeviceInfo]:
    alsa = {
        (0, 0): alsa_entry(0, 0, "PCH", "ALC897 Analog", {"capture", "playback"}),
        (0, 1): alsa_entry(0, 1, "PCH", "ALC897 Digital", {"playback"}),
        (3, 0): alsa_entry(3, 0, "SPK", "micArray RAW SPK", {"capture", "playback"}),
    }
    # Simulate the real failure mode: PortAudio reports ALC897 output=0 while
    # PipeWire owns it, but ALSA aplay -l still proves physical playback exists.
    return [
        audio_io._make_device(0, pa_item("HDA Intel PCH: ALC897 Analog (hw:0,0)", 2, 0), "ALSA", alsa),
        audio_io._make_device(1, pa_item("HDA Intel PCH: ALC897 Digital (hw:0,1)", 0, 2), "ALSA", alsa),
        audio_io._make_device(8, pa_item("micArray RAW SPK: USB Audio (hw:3,0)", 8, 0, 48_000), "ALSA", alsa),
        audio_io._make_device(15, pa_item("pipewire", 64, 64), "ALSA", alsa),
    ]


def test_alsa_identity_survives_portaudio_index_and_busy_capability() -> None:
    analog = sample_devices()[0]
    assert analog.stable_name == "alsa:PCH:0"
    assert analog.alsa_stable_hw == "plughw:CARD=PCH,DEV=0"
    assert analog.max_output_channels == 2
    assert analog.is_analog_output
    assert not analog.is_digital_output


def test_recommendations_choose_uma_input_and_analog_output() -> None:
    devices = sample_devices()
    assert audio_io.recommend_input(devices).stable_name == "alsa:SPK:0"
    assert audio_io.recommend_output(devices).stable_name == "alsa:PCH:0"


def test_digital_virtual_and_uma_output_are_not_recommended() -> None:
    devices = sample_devices()
    selected = audio_io.recommend_output(devices)
    assert not selected.is_digital_output
    assert not selected.is_virtual
    assert not selected.is_uma8


def test_safe_test_tone_is_low_level_and_left_then_right(monkeypatch) -> None:
    devices = sample_devices()
    analog = devices[0]
    captured: dict[str, np.ndarray] = {}
    monkeypatch.setattr(audio_io, "resolve_device_info", lambda *_args, **_kwargs: analog)
    monkeypatch.setattr(audio_io, "list_audio_devices", lambda: devices)
    monkeypatch.setattr(audio_io, "_check_output_only", lambda _info: 2)
    monkeypatch.setattr(
        audio_io, "_play_output",
        lambda playback, _info, _channels: captured.setdefault("playback", playback.copy()),
    )
    returned = audio_io.play_safe_output_test(analog.stable_name)
    playback = captured["playback"]
    assert returned == analog
    assert np.max(np.abs(playback)) <= 0.051
    assert np.any(playback[:10_560, 0])
    assert not np.any(playback[:10_560, 1])
    assert np.any(playback[-10_560:, 1])
    assert not np.any(playback[-10_560:, 0])


def test_continuous_output_pump_keeps_silence_and_splices_probe_once() -> None:
    written: list[np.ndarray] = []
    probe_seen = threading.Event()

    def sink(block: np.ndarray) -> None:
        written.append(block.copy())
        if sum(np.count_nonzero(item[:, 1]) for item in written) >= 300:
            probe_seen.set()
        time.sleep(0.0002)

    output = audio_io._ContinuousOutputSession(sink, 2, block_frames=64)
    try:
        time.sleep(0.002)
        playback = np.zeros((300, 2), dtype=np.float32)
        playback[:, 1] = 0.25
        output.write(playback)
        assert probe_seen.wait(timeout=1.0)
    finally:
        output.close()
    rendered = np.concatenate(written, axis=0)
    assert np.count_nonzero(rendered[:, 0]) == 0
    assert np.count_nonzero(rendered[:, 1]) == 300
    assert np.all(rendered[rendered[:, 1] != 0, 1] == 0.25)


def test_continuous_alsa_command_has_bounded_latency() -> None:
    command = audio_io._continuous_alsa_command(sample_devices()[0], 2)
    assert "--period-time=10000" in command
    assert "--buffer-time=50000" in command
    assert "--avail-min=10000" in command
