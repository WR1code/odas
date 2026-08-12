from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import struct
import threading
from typing import Any

import numpy as np

from .config import SAMPLE_RATE
from .config import CHANNELS
from .output_paths import validate_output_root
from .wav_utils import write_float32, write_pcm16


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"不支持 JSON 序列化：{type(value).__name__}")


def create_experiment_directory(root: Path) -> Path:
    root = validate_output_root(root, create=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = root / stamp
    suffix = 1
    while candidate.exists():
        candidate = root / f"{stamp}_{suffix:02d}"
        suffix += 1
    candidate.mkdir()
    (candidate / "rir").mkdir()
    (candidate / "plots").mkdir()
    return candidate


def write_experiment(
    directory: Path,
    recording: np.ndarray,
    c1: np.ndarray,
    c2: np.ndarray,
    rirs: np.ndarray,
    result: dict[str, Any],
    udp_lines: list[str],
    log_lines: list[str],
) -> None:
    # Keep the historical single-capture layout, but preserve low-amplitude
    # content in the authoritative raw/RIR files.
    write_float32(directory / "raw_linux_8ch.wav", recording)
    write_pcm16(directory / "c1_used.wav", c1)
    write_pcm16(directory / "c2_used.wav", c2)
    for channel in range(rirs.shape[1]):
        write_float32(directory / "rir" / f"rir_ch{channel}.wav", rirs[:, channel])
        write_float32(directory / "rir" / f"rir_float32_ch{channel}.wav", rirs[:, channel])
    np.save(directory / "rir" / "rir_float32.npy", np.asarray(rirs, dtype=np.float32))
    (directory / "android_udp.jsonl").write_text(
        "\n".join(udp_lines) + ("\n" if udp_lines else ""), encoding="utf-8"
    )
    (directory / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "run.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")


class Float32WavStream:
    """Small streaming IEEE-float WAV writer used by unbounded sessions."""

    def __init__(self, path: Path, channels: int, sample_rate: int = SAMPLE_RATE):
        self.path = path
        self.channels = channels
        self.sample_rate = sample_rate
        self.frames = 0
        self._last_header_checkpoint = 0
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w+b")
        self._write_header(0)

    def _write_header(self, data_bytes: int) -> None:
        byte_rate = self.sample_rate * self.channels * 4
        block_align = self.channels * 4
        # WAVE_FORMAT_IEEE_FLOAT (3), standard 16-byte fmt chunk.
        header = (
            b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 3, self.channels, self.sample_rate,
                                     byte_rate, block_align, 32)
            + b"data" + struct.pack("<I", data_bytes)
        )
        self._file.seek(0)
        self._file.write(header)

    def write(self, block: np.ndarray) -> None:
        values = np.ascontiguousarray(block, dtype="<f4")
        if values.ndim != 2 or values.shape[1] != self.channels:
            raise ValueError("连续 WAV block 通道数不匹配")
        with self._lock:
            self._file.seek(0, 2)
            self._file.write(values.tobytes())
            self.frames += values.shape[0]
            if self.frames - self._last_header_checkpoint >= self.sample_rate:
                end = self._file.tell()
                self._write_header(self.frames * self.channels * 4)
                self._file.seek(end)
                self._file.flush()
                self._last_header_checkpoint = self.frames

    def close(self) -> None:
        with self._lock:
            if self._file.closed:
                return
            data_bytes = self.frames * self.channels * 4
            self._write_header(data_bytes)
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()


def repair_float32_wav_header(path: Path, channels: int = CHANNELS) -> int:
    """Recover the RIFF/data sizes of an interrupted project Float32 WAV."""
    size = path.stat().st_size
    if size < 44 or (size - 44) % (channels * 4):
        raise ValueError(f"无法恢复 WAV，文件长度或通道数不合法：{path}")
    data_bytes = size - 44
    if data_bytes > 0xFFFFFFFF - 36:
        raise ValueError("WAV 超过 RIFF 4 GiB 限制；需要切分会话原始录音")
    with path.open("r+b") as stream:
        stream.seek(4)
        stream.write(struct.pack("<I", 36 + data_bytes))
        stream.seek(40)
        stream.write(struct.pack("<I", data_bytes))
        stream.flush()
        os.fsync(stream.fileno())
    return data_bytes // (channels * 4)


class SessionWriter:
    def __init__(self, root: Path, session_id: str | None = None):
        root = validate_output_root(root, create=True)
        self.session_id = session_id or secrets.token_hex(8)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = root / f"{stamp}_{self.session_id}"
        suffix = 1
        while candidate.exists():
            candidate = root / f"{stamp}_{self.session_id}_{suffix:02d}"
            suffix += 1
        self.directory = candidate
        for relative in ("raw", "measurements", "logs"):
            (candidate / relative).mkdir(parents=True, exist_ok=True)
        self.measurements_path = candidate / "measurements.jsonl"
        self.measurements_path.touch()
        self.raw = Float32WavStream(candidate / "raw" / "continuous_float32.wav", CHANNELS)
        self._session: dict[str, Any] = {}
        self._log_lines: list[str] = []
        self._udp_lines: list[str] = []

    def update_session(self, values: dict[str, Any]) -> None:
        self._session.update(values)
        _atomic_json(self.directory / "session.json", self._session)

    def append_log(self, line: str) -> None:
        self._log_lines.append(line)
        (self.directory / "logs" / "run.log").write_text(
            "\n".join(self._log_lines) + "\n", encoding="utf-8"
        )

    def append_udp(self, lines: list[str]) -> None:
        if len(lines) <= len(self._udp_lines):
            return
        self._udp_lines.extend(lines[len(self._udp_lines):])
        (self.directory / "logs" / "android_udp.jsonl").write_text(
            "\n".join(self._udp_lines) + "\n", encoding="utf-8"
        )

    def write_measurement(self, measurement_id: int, result: dict[str, Any], rirs: np.ndarray) -> Path:
        directory = self.directory / "measurements" / f"{measurement_id:06d}"
        plots = directory / "plots"
        plots.mkdir(parents=True, exist_ok=True)
        _atomic_json(directory / "result.json", result)
        for channel in range(rirs.shape[1]):
            write_float32(directory / f"rir_float32_ch{channel}.wav", rirs[:, channel])
        np.save(directory / "rir_float32.npy", np.asarray(rirs, dtype=np.float32))
        with self.measurements_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, default=_json_default) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return directory

    def close(self) -> None:
        self.raw.close()
        self.update_session({"end_timestamp": datetime.now(timezone.utc).isoformat()})
