from __future__ import annotations

from datetime import datetime, timezone
import json
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .audio_io import (
    _check_output_only, _open_input_capture, _play_output,
    resolve_device_info,
)
from .config import CHANNELS, SAMPLE_RATE
from .matched_filter import channel_status, detect_multichannel
from .network_info import network_snapshot
from .wav_utils import load_probe


UDP_TEST_PROTOCOL = "AVTWIN_UDP_TEST_V1"


def test_udp_roundtrip(
    local_host: str, local_port: int, remote_host: str, remote_port: int,
    *, timeout: float = 2.0,
) -> dict[str, Any]:
    """Send a nonce ping and require a matching reply on the configured local port."""
    if not remote_host.strip():
        raise ValueError("请先填写远端 IP")
    if not 0 < local_port <= 65535 or not 0 < remote_port <= 65535:
        raise ValueError("UDP 端口必须在 1..65535 内")
    if timeout <= 0:
        raise ValueError("UDP 测试超时必须大于 0 秒")
    nonce = secrets.token_hex(12)
    started_ns = time.monotonic_ns()
    ping = {
        "protocol": UDP_TEST_PROTOCOL,
        "type": "udp_test_ping",
        "nonce": nonce,
        "sent_monotonic_ns": started_ns,
        "sent_utc": datetime.now(timezone.utc).isoformat(),
        "reply_to_port": local_port,
    }
    received_packets: list[dict[str, Any]] = []
    routed_source_ip = network_snapshot(remote_host).get("source_ip") or local_host
    displayed_local_endpoint = f"{routed_source_ip}:{local_port}"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        channel.bind((local_host, local_port))
        channel.settimeout(min(0.2, timeout))
        channel.sendto(
            json.dumps(ping, ensure_ascii=False, separators=(",", ":")).encode(),
            (remote_host, remote_port),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payload, source = channel.recvfrom(65535)
            except socket.timeout:
                continue
            received_ns = time.monotonic_ns()
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                received_packets.append({"source": list(source), "valid_json": False})
                continue
            if not isinstance(message, dict):
                received_packets.append({"source": list(source), "valid_json": True, "matched": False})
                continue
            # Also act as an echo endpoint, so an Android-initiated ping can
            # verify its reverse path while this test socket is open.
            if message.get("protocol") == UDP_TEST_PROTOCOL and message.get("type") == "udp_test_ping":
                reply = {
                    "protocol": UDP_TEST_PROTOCOL, "type": "udp_test_reply",
                    "nonce": message.get("nonce"), "receiver": "linux",
                }
                channel.sendto(json.dumps(reply, separators=(",", ":")).encode(), source)
                received_packets.append({"source": list(source), "type": "udp_test_ping", "replied": True})
                continue
            matched = (
                message.get("protocol") == UDP_TEST_PROTOCOL
                and message.get("type") == "udp_test_reply"
                and message.get("nonce") == nonce
            )
            received_packets.append({
                "source": list(source), "type": message.get("type"),
                "nonce": message.get("nonce"), "matched": matched,
            })
            if matched:
                return {
                    "success": True,
                    "local_endpoint": displayed_local_endpoint,
                    "remote_endpoint": f"{remote_host}:{remote_port}",
                    "reply_source": f"{source[0]}:{source[1]}",
                    "nonce": nonce,
                    "roundtrip_ms": (received_ns - started_ns) / 1e6,
                    "sent": ping,
                    "received": message,
                    "observed_packets": received_packets,
                }
    return {
        "success": False,
        "local_endpoint": displayed_local_endpoint,
        "remote_endpoint": f"{remote_host}:{remote_port}",
        "nonce": nonce,
        "timeout_s": timeout,
        "failure_reason": (
            "Android 可达路由已选择，但未收到匹配 nonce 的 udp_test_reply；"
            "请保持 Android v0.9.2+ 应用打开，确认控制端口和应用内 Linux Wi-Fi IP"
        ),
        "observed_packets": received_packets,
    }


def test_probe_playback(
    path: Path, output_device: int | str, output_channel: int | str,
    playback_gain: float,
) -> dict[str, Any]:
    reference, warnings = load_probe(path, warning=lambda _message: None)
    info = resolve_device_info(output_device, input_device=False)
    channels = _check_output_only(info)
    rendered = np.clip(reference * playback_gain, -1.0, 1.0)
    playback = np.zeros((reference.size, channels), dtype=np.float32)
    if output_channel == "both":
        playback[:] = rendered[:, None]
    else:
        playback[:, int(output_channel)] = rendered
    _play_output(playback, info, channels)
    return {
        "frames": reference.size, "duration_s": reference.size / SAMPLE_RATE,
        "device": info.display_name, "output_channel": output_channel,
        "peak_fs": float(np.max(np.abs(rendered))), "warnings": warnings,
    }


def test_uma8_recording(
    input_device: int | str, *, duration: float = 1.0,
    audio_block: Callable[[np.ndarray], None] | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    info = resolve_device_info(input_device, input_device=True)
    blocks: list[np.ndarray] = []
    progress = threading.Event()

    def accept(block: np.ndarray, _warning: str | None = None) -> None:
        values = np.ascontiguousarray(block, dtype=np.float32)
        blocks.append(values)
        if audio_block:
            audio_block(values)
        progress.set()

    target = round(duration * SAMPLE_RATE)
    with _open_input_capture(info, accept) as session:
        while sum(block.shape[0] for block in blocks) < target:
            if session.finished.is_set():
                raise RuntimeError(session.errors[-1] if session.errors else "UMA-8 input ended")
            progress.wait(0.1)
            progress.clear()
    recording = np.concatenate(blocks, axis=0)[:target]
    statuses = channel_status(recording)
    return recording, {
        "frames": recording.shape[0], "duration_s": recording.shape[0] / SAMPLE_RATE,
        "levels": np.max(np.abs(recording), axis=0).tolist(),
        "valid_channels": [int(key) for key, value in statuses.items() if value == "active"],
        "inactive_channels": [int(key) for key, value in statuses.items() if value == "inactive_zero"],
    }


def test_probe_detector(
    path: Path, input_device: int | str, threshold: float, min_channels: int,
    *, duration: float = 3.0,
    audio_block: Callable[[np.ndarray], None] | None = None,
) -> dict[str, Any]:
    reference, _warnings = load_probe(path, warning=lambda _message: None)
    recording, capture = test_uma8_recording(
        input_device, duration=duration, audio_block=audio_block,
    )
    result = detect_multichannel(
        recording, reference, threshold, channel_status(recording),
    )
    result["consensus_passed"] = result["channels_passed"] >= min_channels
    result["capture"] = capture
    return result
