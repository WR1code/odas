from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from contextlib import contextmanager
import fcntl
import json
import re
import subprocess
import threading
import time
from typing import Any, Callable

import numpy as np
from scipy import signal

from .config import CHANNELS, SAMPLE_RATE
from .matched_filter import detect_multichannel


_HW_RE = re.compile(r"\(hw:(\d+),(\d+)\)")
_ALSA_LINE_RE = re.compile(
    r"^card\s+(?P<card>\d+):\s+(?P<card_id>\S+)\s+\[(?P<card_name>[^]]+)\],\s+"
    r"device\s+(?P<device>\d+):\s+(?P<device_name>.*?)\s*\[(?P<device_label>[^]]+)\]$"
)
_DIGITAL_WORDS = ("digital", "iec958", "s/pdif", "spdif", "hdmi")
_VIRTUAL_NAMES = ("default", "sysdefault", "pipewire", "pulse", "dmix")


@dataclass(frozen=True, slots=True)
class AudioDeviceInfo:
    display_name: str
    backend: str
    portaudio_index: int
    hostapi: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    alsa_card: int | None
    alsa_card_id: str | None
    alsa_device: int | None
    alsa_hw: str | None
    alsa_stable_hw: str | None
    stable_name: str
    is_input_candidate: bool
    is_output_candidate: bool
    is_analog_output: bool
    is_digital_output: bool
    is_virtual: bool
    is_uma8: bool
    alsa_has_capture: bool
    alsa_has_playback: bool

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        # Retain the historical result.json key for compatibility.
        result["index"] = self.portaudio_index
        result["name"] = self.display_name
        return result


def _sd():
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "无法加载 PortAudio/sounddevice；请使用项目的 ./avtwin_linux/run_acoustic_handshake.sh 启动"
        ) from exc
    return sd


def _alsa_hardware() -> dict[tuple[int, int], dict[str, Any]]:
    hardware: dict[tuple[int, int], dict[str, Any]] = {}
    for command, direction in (("aplay", "playback"), ("arecord", "capture")):
        try:
            completed = subprocess.run(
                [command, "-l"], capture_output=True, text=True, check=False, timeout=3.0
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        for raw_line in completed.stdout.splitlines():
            match = _ALSA_LINE_RE.match(raw_line.strip())
            if not match:
                continue
            values = match.groupdict()
            key = (int(values["card"]), int(values["device"]))
            entry = hardware.setdefault(key, {
                "card": key[0],
                "device": key[1],
                "card_id": values["card_id"],
                "card_name": values["card_name"],
                "device_name": values["device_name"].strip(),
                "device_label": values["device_label"],
                "directions": set(),
            })
            entry["directions"].add(direction)
    return hardware


def _friendly_name(name: str, alsa: dict[str, Any] | None) -> str:
    combined = " ".join((name, alsa.get("device_name", "") if alsa else "")).casefold()
    if "alc897" in combined and "alt analog" in combined:
        return "HDA Intel PCH: ALC897 Alt Analog (capture)"
    if "alc897" in combined and "analog" in combined:
        return "HDA Intel PCH: ALC897 Analog / 3.5mm Output"
    if "micarray raw spk" in combined:
        return "UMA-8 micArray RAW SPK"
    return name


def _make_device(index: int, item: Any, hostapi_name: str, alsa_map: dict[tuple[int, int], dict[str, Any]]) -> AudioDeviceInfo:
    raw_name = str(item["name"])
    match = _HW_RE.search(raw_name)
    card = int(match.group(1)) if match else None
    device = int(match.group(2)) if match else None
    alsa = alsa_map.get((card, device)) if card is not None and device is not None else None
    card_id = str(alsa["card_id"]) if alsa else None
    text = " ".join((raw_name, str(alsa or ""))).casefold()
    digital = any(word in text for word in _DIGITAL_WORDS)
    analog = "analog" in text and not digital
    uma8 = "micarray raw spk" in text or "minidsp" in text or "uma-8" in text
    virtual = match is None and any(raw_name.casefold() == word for word in _VIRTUAL_NAMES)
    stable = (
        f"alsa:{card_id}:{device}"
        if card_id is not None and device is not None
        else f"portaudio:{hostapi_name.casefold()}:{raw_name.casefold()}"
    )
    alsa_capture = bool(alsa and "capture" in alsa["directions"])
    alsa_playback = bool(alsa and "playback" in alsa["directions"])
    max_input = max(int(item["max_input_channels"]), CHANNELS if uma8 and alsa_capture else 0)
    # PortAudio reports zero output channels when a direct ALSA PCM is busy.
    # aplay -l is authoritative for physical playback presence; stereo is the
    # experiment contract and is verified by opening plughw at 48 kHz later.
    max_output = max(int(item["max_output_channels"]), 2 if alsa_playback else 0)
    return AudioDeviceInfo(
        display_name=_friendly_name(raw_name, alsa),
        backend=(
            "Direct ALSA"
            if hostapi_name.casefold() == "alsa direct"
            else ("ALSA via PortAudio" if hostapi_name.casefold() == "alsa" else "PortAudio")
        ),
        portaudio_index=index,
        hostapi=hostapi_name,
        max_input_channels=max_input,
        max_output_channels=max_output,
        default_samplerate=float(item["default_samplerate"]),
        alsa_card=card,
        alsa_card_id=card_id,
        alsa_device=device,
        alsa_hw=f"hw:{card},{device}" if card is not None and device is not None else None,
        alsa_stable_hw=f"plughw:CARD={card_id},DEV={device}" if card_id is not None and device is not None else None,
        stable_name=stable,
        is_input_candidate=max_input >= CHANNELS,
        is_output_candidate=max_output >= 2,
        is_analog_output=analog,
        is_digital_output=digital,
        is_virtual=virtual,
        is_uma8=uma8,
        alsa_has_capture=alsa_capture,
        alsa_has_playback=alsa_playback,
    )


def list_audio_devices() -> list[AudioDeviceInfo]:
    sd = _sd()
    alsa_map = _alsa_hardware()
    hostapis = sd.query_hostapis()
    result: list[AudioDeviceInfo] = []
    mapped_hardware: set[tuple[int, int]] = set()
    for index, item in enumerate(sd.query_devices()):
        hostapi_index = int(item["hostapi"])
        hostapi_name = str(hostapis[hostapi_index]["name"])
        device = _make_device(index, item, hostapi_name, alsa_map)
        result.append(device)
        if device.alsa_card is not None and device.alsa_device is not None:
            mapped_hardware.add((device.alsa_card, device.alsa_device))
    for key, alsa in alsa_map.items():
        if key in mapped_hardware:
            continue
        directions = alsa["directions"]
        is_uma = "micarray raw spk" in str(alsa["card_name"]).casefold()
        synthetic_item = {
            "name": (
                f"{alsa['card_name']}: {alsa['device_name']} "
                f"(hw:{alsa['card']},{alsa['device']})"
            ),
            "max_input_channels": CHANNELS if is_uma and "capture" in directions else (2 if "capture" in directions else 0),
            "max_output_channels": 2 if "playback" in directions else 0,
            "default_samplerate": SAMPLE_RATE,
        }
        result.append(_make_device(-1, synthetic_item, "ALSA direct", alsa_map))
    return result


def list_devices() -> list[dict[str, Any]]:
    """Compatibility dictionary view used by the CLI and existing callers."""
    return [device.to_dict() for device in list_audio_devices()]


def recommend_input(devices: list[AudioDeviceInfo]) -> AudioDeviceInfo | None:
    candidates = [item for item in devices if item.is_input_candidate]
    return next((item for item in candidates if item.is_uma8), candidates[0] if candidates else None)


def recommend_output(devices: list[AudioDeviceInfo]) -> AudioDeviceInfo | None:
    candidates = [item for item in devices if item.is_output_candidate]
    preferred = [
        item for item in candidates
        if item.is_analog_output and not item.is_digital_output and not item.is_virtual and not item.is_uma8
    ]
    if preferred:
        return preferred[0]
    safe_physical = [
        item for item in candidates
        if not item.is_digital_output and not item.is_virtual and not item.is_uma8
    ]
    return safe_physical[0] if safe_physical else None


def resolve_device_info(value: int | str | None, *, input_device: bool) -> AudioDeviceInfo:
    devices = list_audio_devices()
    minimum = CHANNELS if input_device else 2
    channel = lambda item: item.max_input_channels if input_device else item.max_output_channels
    if value is None or value == "":
        recommended = recommend_input(devices) if input_device else recommend_output(devices)
        if recommended is not None:
            return recommended
        raise ValueError("找不到安全的推荐设备，请明确选择设备")
    try:
        index = int(value)
    except (TypeError, ValueError):
        matches = [
            item for item in devices
            if (
                str(value).casefold() == item.stable_name.casefold()
                or str(value).casefold() in item.display_name.casefold()
                or str(value).casefold() == (item.alsa_stable_hw or "").casefold()
            )
            and channel(item) >= minimum
        ]
        if len(matches) != 1:
            raise ValueError(
                f"稳定设备名 {value!r} 匹配到 {[item.stable_name for item in matches]}；"
                "请使用 --list-devices 查看 stable name"
            )
        return matches[0]
    runtime_matches = [item for item in devices if item.portaudio_index == index]
    if len(runtime_matches) != 1:
        raise ValueError(f"设备 index {index} 不存在")
    if channel(runtime_matches[0]) < minimum:
        raise ValueError(
            f"设备 {runtime_matches[0].display_name} 的通道数不足：{channel(runtime_matches[0])}"
        )
    return runtime_matches[0]


def resolve_device(value: int | str | None, *, input_device: bool) -> int:
    """Compatibility runtime handle; direct ALSA-only devices return -1."""
    return resolve_device_info(value, input_device=input_device).portaudio_index


def device_metadata(index: int) -> dict[str, Any]:
    matches = [item for item in list_audio_devices() if item.portaudio_index == index]
    if len(matches) != 1:
        raise ValueError(f"PortAudio runtime index {index} 不存在或不唯一")
    return matches[0].to_dict()


def selected_device_info(value: int | str | None, *, input_device: bool) -> AudioDeviceInfo:
    return resolve_device_info(value, input_device=input_device)


def output_warnings(device: AudioDeviceInfo) -> list[str]:
    warnings: list[str] = []
    if device.is_digital_output:
        warnings.append("WARNING: selected output is digital S/PDIF/HDMI, not the ALC897 3.5 mm analog output.")
    if device.is_uma8:
        warnings.append("WARNING: UMA-8 is selected as playback device. Expected ALC897 Analog / 3.5 mm output.")
    if device.is_virtual:
        warnings.append("WARNING: selected output is a virtual/default route and may change with PipeWire routing.")
    return warnings


def check_audio_configuration(
    input_device: AudioDeviceInfo, output_device: AudioDeviceInfo,
    output_channel: int | str = 0,
) -> tuple[int, int]:
    sd = _sd()
    if input_device.stable_name == output_device.stable_name:
        raise ValueError("输入和输出设备必须分离；不能同时使用 UMA-8 录音和播放")
    input_info = input_device
    output_info = output_device
    if input_info.max_input_channels < CHANNELS:
        raise ValueError("所选输入设备不支持 8 通道")
    output_channels = min(2, output_info.max_output_channels)
    if output_channel != "both" and (
        not isinstance(output_channel, int) or output_channel < 0 or output_channel >= output_channels
    ):
        raise ValueError(f"输出声道必须在 0..{output_channels - 1} 内")
    if output_channel == "both" and output_channels < 2:
        raise ValueError("BOTH 输出需要双声道输出设备")
    try:
        if input_info.alsa_has_capture and input_info.alsa_stable_hw:
            _check_alsa_input(input_info)
        else:
            sd.check_input_settings(
                device=input_info.portaudio_index, samplerate=SAMPLE_RATE,
                channels=CHANNELS, dtype="float32",
            )
    except Exception as exc:
        raise RuntimeError(
            f"无法以 {SAMPLE_RATE} Hz / {CHANNELS} ch 打开 UMA-8 输入 "
            f"{input_info.alsa_stable_hw or input_info.stable_name}: {exc}"
        ) from exc
    _check_output_device(output_info, output_channels)
    return CHANNELS, output_channels


def play_safe_output_test(
    device_selector: int | str,
    *,
    notify: Callable[[str], None] | None = None,
) -> AudioDeviceInfo:
    """Play short 5%-scale tones on left then right; no recording is started."""
    notify = notify or (lambda _message: None)
    info = resolve_device_info(device_selector, input_device=False)
    channels = _check_output_only(info)
    for warning in output_warnings(info):
        notify(warning)
    notify(
        f"测试输出实际设备：{info.display_name} | {info.alsa_stable_hw or info.stable_name} | "
        f"PortAudio runtime index {info.portaudio_index if info.portaudio_index >= 0 else 'not used'} | {SAMPLE_RATE} Hz"
    )
    tone_frames = round(0.22 * SAMPLE_RATE)
    gap_frames = round(0.12 * SAMPLE_RATE)
    t = np.arange(tone_frames, dtype=np.float64) / SAMPLE_RATE
    envelope = signal.windows.tukey(tone_frames, alpha=0.25)
    playback = np.zeros((tone_frames * 2 + gap_frames, channels), dtype=np.float32)
    playback[:tone_frames, 0] = np.asarray(0.05 * envelope * np.sin(2 * np.pi * 440.0 * t), np.float32)
    right = 1 if channels >= 2 else 0
    playback[tone_frames + gap_frames :, right] = np.asarray(
        0.05 * envelope * np.sin(2 * np.pi * 660.0 * t), np.float32
    )
    _play_output(playback, info, channels)
    notify("测试输出完成：左声道 440 Hz，随后右声道 660 Hz（5% 数字幅度）")
    return info


def _check_output_only(info: AudioDeviceInfo) -> int:
    channels = min(2, info.max_output_channels)
    if channels < 1:
        raise ValueError("所选设备没有输出通道")
    _check_output_device(info, channels)
    return channels


def _alsa_command(info: AudioDeviceInfo, channels: int) -> list[str]:
    if not info.alsa_stable_hw:
        raise ValueError("设备没有稳定 ALSA PCM identity")
    return [
        "aplay", "-q", "-D", info.alsa_stable_hw,
        "-t", "raw", "-f", "FLOAT_LE", "-r", str(SAMPLE_RATE), "-c", str(channels),
    ]


def _continuous_alsa_command(info: AudioDeviceInfo, channels: int) -> list[str]:
    # Bound hardware latency while the output pump prevents underruns. Without
    # these limits, aplay plus a dynamically grown pipe can queue seconds of
    # silence ahead of a manually requested C1.
    return _alsa_command(info, channels) + [
        "--period-time=10000", "--buffer-time=50000", "--avail-min=10000",
    ]


def _arecord_command(info: AudioDeviceInfo) -> list[str]:
    if not info.alsa_stable_hw:
        raise ValueError("输入设备没有稳定 ALSA PCM identity")
    return [
        "arecord", "-q", "-D", info.alsa_stable_hw,
        "-t", "raw", "-f", "FLOAT_LE", "-r", str(SAMPLE_RATE), "-c", str(CHANNELS),
    ]


def _alsa_error(info: AudioDeviceInfo, stderr: str) -> RuntimeError:
    detail = stderr.strip() or "ALSA output open failed"
    return RuntimeError(
        f"无法以 {SAMPLE_RATE} Hz 打开 {info.alsa_stable_hw}: {detail}. "
        "程序已尝试临时释放匹配的 PipeWire card；如果仍包含 'Device or resource busy'，"
        "说明还有其他程序直接占用该 PCM，请关闭占用程序后重试。"
    )


def _pipewire_sink(info: AudioDeviceInfo) -> tuple[str, bool] | None:
    """Return matching PipeWire sink name and whether it was already suspended."""
    if info.alsa_card_id is None or info.alsa_device is None:
        return None
    try:
        completed = subprocess.run(
            ["pactl", "-f", "json", "list", "sinks"],
            capture_output=True, text=True, check=False, timeout=3.0,
        )
        sinks = json.loads(completed.stdout) if completed.returncode == 0 else []
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    for sink in sinks:
        properties = sink.get("properties", {})
        if (
            str(properties.get("alsa.id", "")).casefold() == info.alsa_card_id.casefold()
            and str(properties.get("alsa.device", "")) == str(info.alsa_device)
        ):
            return str(sink["name"]), str(sink.get("state", "")).upper() == "SUSPENDED"
    return None


def _pipewire_card(info: AudioDeviceInfo) -> tuple[str, str] | None:
    if info.alsa_card_id is None:
        return None
    try:
        completed = subprocess.run(
            ["pactl", "-f", "json", "list", "cards"],
            capture_output=True, text=True, check=False, timeout=3.0,
        )
        cards = json.loads(completed.stdout) if completed.returncode == 0 else []
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    for card in cards:
        properties = card.get("properties", {})
        if str(properties.get("alsa.id", "")).casefold() == info.alsa_card_id.casefold():
            active = card.get("active_profile", "")
            if isinstance(active, dict):
                active = active.get("name", "")
            return str(card["name"]), str(active)
    return None


@contextmanager
def _direct_alsa_access(info: AudioDeviceInfo):
    """Temporarily release only the matching PipeWire card, then restore it."""
    card = _pipewire_card(info)
    profile_disabled_here = False
    if card is not None and card[1] and card[1] != "off":
        completed = subprocess.run(
            ["pactl", "set-card-profile", card[0], "off"],
            capture_output=True, text=True, check=False, timeout=3.0,
        )
        if completed.returncode == 0:
            profile_disabled_here = True
            time.sleep(0.12)
    sink = None if profile_disabled_here else _pipewire_sink(info)
    suspended_here = False
    if sink is not None and not sink[1]:
        completed = subprocess.run(
            ["pactl", "suspend-sink", sink[0], "1"],
            capture_output=True, text=True, check=False, timeout=3.0,
        )
        if completed.returncode == 0:
            suspended_here = True
            time.sleep(0.12)
    try:
        yield
    finally:
        if suspended_here:
            subprocess.run(
                ["pactl", "suspend-sink", sink[0], "0"],
                capture_output=True, text=True, check=False, timeout=3.0,
            )
        if profile_disabled_here:
            subprocess.run(
                ["pactl", "set-card-profile", card[0], card[1]],
                capture_output=True, text=True, check=False, timeout=3.0,
            )


def _check_alsa_input(info: AudioDeviceInfo) -> None:
    """Open direct ALSA capture and read a tiny block; no audio is retained."""
    try:
        with _direct_alsa_access(info):
            process = subprocess.Popen(
                _arecord_command(info), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=-1,
            )
            try:
                assert process.stdout is not None
                payload = process.stdout.read(CHANNELS * 4 * 64)
                if len(payload) < CHANNELS * 4:
                    stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
                    raise RuntimeError(stderr.strip() or "ALSA capture returned no PCM")
            finally:
                process.terminate()
                if process.stdout is not None:
                    process.stdout.close()
                try:
                    process.wait(timeout=0.75)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ALSA direct input self-check failed: {exc}") from exc


def _raise_if_input_ended(session: _InputSession) -> None:
    if session.finished.is_set():
        raise RuntimeError(session.errors[-1] if session.errors else "ALSA input stream ended unexpectedly")


@contextmanager
def _open_input_capture(
    info: AudioDeviceInfo,
    accept_block: Callable[[np.ndarray, str | None], None],
):
    if info.alsa_has_capture and info.alsa_stable_hw:
        finished = threading.Event()
        errors: list[str] = []
        stopping = threading.Event()
        with _direct_alsa_access(info):
            try:
                process = subprocess.Popen(
                    _arecord_command(info), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=-1,
                )
            except (FileNotFoundError, OSError) as exc:
                raise RuntimeError(f"无法启动 ALSA 8 通道录音：{exc}") from exc

            def reader() -> None:
                block_bytes = 2048 * CHANNELS * 4
                remainder = b""
                try:
                    assert process.stdout is not None
                    while not stopping.is_set():
                        payload = process.stdout.read(block_bytes)
                        if not payload:
                            break
                        payload = remainder + payload
                        usable = len(payload) - len(payload) % (CHANNELS * 4)
                        remainder = payload[usable:]
                        if usable:
                            block = np.frombuffer(payload[:usable], dtype="<f4").reshape(-1, CHANNELS)
                            accept_block(block.copy(), None)
                except Exception as exc:
                    errors.append(f"ALSA input reader failed: {exc}")
                finally:
                    if not stopping.is_set():
                        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
                        errors.append(stderr.strip() or "ALSA arecord stopped unexpectedly")
                    finished.set()

            thread = threading.Thread(target=reader, name="avtwin-alsa-input", daemon=True)
            thread.start()
            # Detect immediate ALSA open errors before reporting Recording started.
            time.sleep(0.08)
            if process.poll() is not None:
                thread.join(timeout=1.0)
                raise RuntimeError(errors[-1] if errors else "ALSA arecord failed to start")
            try:
                yield _InputSession(finished, errors)
            finally:
                stopping.set()
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
                thread.join(timeout=2.0)
        return

    sd = _sd()
    finished = threading.Event()
    errors: list[str] = []

    def callback(indata: np.ndarray, _frames: int, _time: Any, status: Any) -> None:
        accept_block(indata.copy(), str(status) if status else None)

    try:
        with sd.InputStream(
            device=info.portaudio_index, samplerate=SAMPLE_RATE,
            channels=CHANNELS, dtype="float32", blocksize=0,
            latency="high", callback=callback,
        ):
            yield _InputSession(finished, errors)
    finally:
        finished.set()


def _check_output_device(info: AudioDeviceInfo, channels: int) -> None:
    if info.alsa_has_playback and info.alsa_stable_hw:
        try:
            with _direct_alsa_access(info):
                completed = subprocess.run(
                    _alsa_command(info, channels), input=b"", capture_output=True,
                    check=False, timeout=3.0,
                )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"ALSA 48 kHz 输出自检失败：{exc}") from exc
        if completed.returncode != 0:
            raise _alsa_error(info, completed.stderr.decode(errors="replace"))
        return
    sd = _sd()
    sd.check_output_settings(
        device=info.portaudio_index, samplerate=SAMPLE_RATE,
        channels=channels, dtype="float32",
    )


def _play_output(playback: np.ndarray, info: AudioDeviceInfo, channels: int) -> None:
    if info.alsa_has_playback and info.alsa_stable_hw:
        try:
            with _direct_alsa_access(info):
                completed = subprocess.run(
                    _alsa_command(info, channels),
                    input=np.ascontiguousarray(playback, dtype="<f4").tobytes(),
                    capture_output=True, check=False,
                    timeout=max(5.0, playback.shape[0] / SAMPLE_RATE + 3.0),
                )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"ALSA direct playback failed: {exc}") from exc
        if completed.returncode != 0:
            raise _alsa_error(info, completed.stderr.decode(errors="replace"))
        return
    sd = _sd()
    with sd.OutputStream(
        device=info.portaudio_index, samplerate=SAMPLE_RATE,
        channels=channels, dtype="float32", latency="high",
    ) as output:
        output.write(playback)


@dataclass(slots=True)
class CaptureResult:
    recording: np.ndarray
    playback_issue_sample: int | None
    live_c2_candidate_sample: int | None
    stream_warnings: list[str]
    interrupted: bool


@dataclass(slots=True)
class _InputSession:
    finished: threading.Event
    errors: list[str]


class ContinuousAudioBackend:
    """Reuse one input stream for a whole multi-measurement session."""

    def __init__(
        self,
        input_device: AudioDeviceInfo,
        output_device: AudioDeviceInfo,
        output_channel: int | str,
        playback_gain: float,
    ):
        self.input_device = input_device
        self.output_device = output_device
        self.output_channel = output_channel
        self.playback_gain = playback_gain
        _inputs, self.output_channels = check_audio_configuration(
            input_device, output_device, output_channel
        )
        self._output_session: _ContinuousOutputSession | None = None

    @contextmanager
    def capture(self, accept_block: Callable[[np.ndarray, str | None], None]):
        # Both physical devices stay open for the full session. The existing
        # direct-ALSA context restores each matching PipeWire profile on exit.
        with _open_continuous_output(self.output_device, self.output_channels) as output:
            self._output_session = output
            try:
                with _open_input_capture(self.input_device, accept_block) as session:
                    yield session
            finally:
                self._output_session = None

    def play(self, probe: np.ndarray) -> None:
        playback = np.zeros((probe.size, self.output_channels), dtype=np.float32)
        rendered = np.clip(probe * self.playback_gain, -1.0, 1.0)
        if self.output_channel == "both":
            playback[:, :] = rendered[:, None]
        else:
            playback[:, int(self.output_channel)] = rendered
        if self._output_session is None:
            raise RuntimeError("持续输出流尚未打开")
        self._output_session.write(playback)


class _ContinuousOutputSession:
    """Feed a persistent output continuously and splice queued probes once."""

    def __init__(
        self, sink_write: Callable[[np.ndarray], None], channels: int,
        *, block_frames: int = 256,
    ):
        self._sink_write = sink_write
        self._channels = channels
        self._block_frames = block_frames
        self._queue: deque[np.ndarray] = deque()
        self._queued_offset = 0
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._started = threading.Event()
        self._errors: list[Exception] = []
        self._thread = threading.Thread(
            target=self._pump, name="avtwin-continuous-output", daemon=True,
        )
        self._thread.start()
        self._started.wait(timeout=1.0)

    def _pump(self) -> None:
        self._started.set()
        try:
            while not self._stop.is_set():
                block = np.zeros((self._block_frames, self._channels), dtype=np.float32)
                filled = 0
                with self._condition:
                    while filled < self._block_frames and self._queue:
                        queued = self._queue[0]
                        count = min(self._block_frames - filled, queued.shape[0] - self._queued_offset)
                        block[filled:filled + count] = queued[
                            self._queued_offset:self._queued_offset + count
                        ]
                        filled += count
                        self._queued_offset += count
                        if self._queued_offset == queued.shape[0]:
                            self._queue.popleft()
                            self._queued_offset = 0
                self._sink_write(block)
        except Exception as exc:
            self._errors.append(exc)
            self._stop.set()

    def write(self, playback: np.ndarray) -> None:
        if self._errors:
            raise RuntimeError(f"持续输出流失败：{self._errors[-1]}") from self._errors[-1]
        values = np.ascontiguousarray(playback, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != self._channels:
            raise ValueError("持续输出 block 通道数不匹配")
        with self._condition:
            self._queue.append(values)
            self._condition.notify_all()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            raise RuntimeError("持续输出泵未能安全停止")
        if self._errors:
            raise RuntimeError(f"持续输出流失败：{self._errors[-1]}") from self._errors[-1]


@contextmanager
def _open_continuous_output(info: AudioDeviceInfo, channels: int):
    if info.alsa_has_playback and info.alsa_stable_hw:
        with _direct_alsa_access(info):
            try:
                process = subprocess.Popen(
                    _continuous_alsa_command(info, channels), stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0,
                )
            except (FileNotFoundError, OSError) as exc:
                raise RuntimeError(f"无法启动持续 ALSA 输出：{exc}") from exc
            time.sleep(0.05)
            if process.poll() is not None:
                stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
                raise _alsa_error(info, stderr)
            if process.stdin is not None:
                try:
                    # One page is 10.7 ms at 48 kHz stereo Float32. This keeps
                    # the writer back-pressured instead of allowing Linux to
                    # grow the pipe toward pipe-max-size (several seconds).
                    fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
                except (AttributeError, OSError):
                    # The explicit ALSA buffer still bounds most latency on
                    # kernels that do not expose F_SETPIPE_SZ.
                    pass

            def write(playback: np.ndarray) -> None:
                if process.poll() is not None or process.stdin is None:
                    stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
                    raise _alsa_error(info, stderr)
                try:
                    process.stdin.write(np.ascontiguousarray(playback, dtype="<f4").tobytes())
                    process.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    raise RuntimeError(f"持续 ALSA 输出失败：{exc}") from exc

            try:
                output = _ContinuousOutputSession(write, channels)
                try:
                    yield output
                finally:
                    output.close()
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1.0)
        return
    sd = _sd()
    with sd.OutputStream(
        device=info.portaudio_index, samplerate=SAMPLE_RATE,
        channels=channels, dtype="float32", latency="high",
    ) as stream:
        output = _ContinuousOutputSession(
            lambda playback: stream.write(np.ascontiguousarray(playback, dtype=np.float32)),
            channels,
        )
        try:
            yield output
        finally:
            output.close()


def capture_handshake(
    c1: np.ndarray,
    c2: np.ndarray,
    *,
    input_device: AudioDeviceInfo,
    output_device: AudioDeviceInfo,
    output_channel: int | str,
    playback_gain: float,
    pre_roll: float,
    reply_timeout: float,
    tail: float,
    c2_threshold: float,
    stop_event: threading.Event | None = None,
    notify: Callable[[str], None] | None = None,
    audio_block: Callable[[np.ndarray], None] | None = None,
    recording_preview: Callable[[np.ndarray, int], None] | None = None,
) -> CaptureResult:
    """Record one uninterrupted input timeline while C1 is played separately."""
    sd = _sd()
    notify = notify or (lambda _message: None)
    stop_event = stop_event or threading.Event()
    blocks: list[np.ndarray] = []
    warnings: list[str] = []
    received = 0
    progress = threading.Event()
    interrupted = False
    issue_sample: int | None = None
    live_c2_sample: int | None = None
    last_preview_frame = 0

    _input_channels, output_channels = check_audio_configuration(
        input_device, output_device, output_channel
    )
    output_info = output_device

    def accept_block(block: np.ndarray, status: str | None = None) -> None:
        nonlocal received
        if status:
            warnings.append(status)
        block = np.ascontiguousarray(block, dtype=np.float32)
        blocks.append(block)
        received += block.shape[0]
        progress.set()
        if audio_block is not None:
            audio_block(block)

    playback = np.zeros((c1.size, output_channels), dtype=np.float32)
    rendered = np.clip(c1 * playback_gain, -1.0, 1.0)
    if output_channel == "both":
        playback[:, :] = rendered[:, None]
    else:
        playback[:, int(output_channel)] = rendered
    pre_frames = round(pre_roll * SAMPLE_RATE)
    tail_frames = round(tail * SAMPLE_RATE)

    try:
        input_info = input_device
        with _open_input_capture(input_info, accept_block) as input_session:
            try:
                if input_session.finished.is_set():
                    raise RuntimeError(input_session.errors[-1] if input_session.errors else "UMA-8 input ended")
                else:
                    notify("[1] Recording started")
                    while received < pre_frames and not stop_event.is_set():
                        progress.wait(0.1)
                        progress.clear()
                        _raise_if_input_ended(input_session)
                    if not stop_event.is_set():
                        notify("[2] Pre-roll complete")
                        issue_sample = received
                        notify("[3] C1 playback issued")
                        _play_output(playback, output_info, output_channels)
                        deadline = issue_sample + c1.size + round(reply_timeout * SAMPLE_RATE)
                        search_cursor = issue_sample + c1.size
                        target = deadline
                        notify("[4] Waiting for Android C2 reply...")
                        while received < target and not stop_event.is_set():
                            progress.wait(0.1)
                            progress.clear()
                            _raise_if_input_ended(input_session)
                            # This processing runs on the controller thread, never in the
                            # PortAudio callback. Offline per-channel analysis remains the
                            # authoritative result; this only ends capture after the tail.
                            latest_start = received - c2.size
                            if live_c2_sample is None and latest_start >= search_cursor:
                                snapshot = np.concatenate(blocks, axis=0)
                                segment_start = search_cursor
                                segment = snapshot[segment_start : latest_start + c2.size]
                                live_status = {
                                    str(channel): ("inactive_zero" if channel == 7 else "active")
                                    for channel in range(segment.shape[1])
                                }
                                live_detection = detect_multichannel(
                                    segment, c2, c2_threshold, live_status
                                )
                                search_cursor = latest_start + 1
                                if (
                                    live_detection["system_sample"] is not None
                                    and live_detection["channels_passed"] >= 2
                                ):
                                    live_c2_sample = segment_start + int(live_detection["system_sample"])
                                    target = max(received, live_c2_sample + c2.size + tail_frames)
                                    notify(f"[5] Live C2 candidate detected near sample {live_c2_sample}; recording tail")
                            if (
                                live_c2_sample is not None
                                and recording_preview is not None
                                and received - last_preview_frame >= round(0.20 * SAMPLE_RATE)
                            ):
                                snapshot = np.concatenate(blocks, axis=0)
                                recording_preview(snapshot, live_c2_sample)
                                last_preview_frame = received
            except sd.PortAudioError as exc:
                raise RuntimeError(
                    f"UMA-8 录音设备无法打开：{input_info.alsa_stable_hw or input_info.stable_name}; {exc}"
                ) from exc
    except KeyboardInterrupt:
        interrupted = True
        stop_event.set()
        notify("Ctrl+C：正在安全关闭并保存已录数据")

    recording = np.concatenate(blocks, axis=0) if blocks else np.zeros((0, CHANNELS), np.float32)
    return CaptureResult(
        recording, issue_sample, live_c2_sample, warnings,
        interrupted or stop_event.is_set(),
    )
