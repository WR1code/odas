from __future__ import annotations

import numpy as np
from scipy.io import wavfile

from avtwin_linux.config import SAMPLE_RATE
from avtwin_linux.wav_utils import load_probe


def test_probe_conversion_warns_and_returns_48k_mono(tmp_path) -> None:
    path = tmp_path / "stereo_44100.wav"
    t = np.arange(4_410) / 44_100
    stereo = np.column_stack((np.sin(2 * np.pi * 500 * t), np.sin(2 * np.pi * 800 * t)))
    wavfile.write(path, 44_100, (stereo * 20_000).astype(np.int16))
    messages: list[str] = []
    probe, warnings = load_probe(path, warning=messages.append)
    assert probe.ndim == 1
    assert abs(probe.size - round(0.1 * SAMPLE_RATE)) <= 1
    assert len(warnings) == 2
    assert all(message.startswith("WARNING:") for message in messages)
