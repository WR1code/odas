"""TCP server for ODAS raw.socket input (ODAS is the TCP client)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import socket
import time


class PCMServer:
    def __init__(self, pcm: Path, host: str, port: int, hop_size: int = 512,
                 channels: int = 8, sample_bytes: int = 4, realtime: bool = True):
        self.pcm = pcm
        self.host = host
        self.port = port
        self.block_bytes = hop_size * channels * sample_bytes
        self.block_seconds = hop_size / 48000.0
        self.realtime = realtime
        self.stop = False
        self.metrics = {"blocks_sent": 0, "dropped_blocks": 0, "underruns": 0,
                        "pacing_deadline_misses": 0, "reconnects": 0,
                        "partial_sends": 0, "bytes_sent": 0, "processing_latency_ms_max": 0.0}

    def send_all(self, client: socket.socket, block: bytes) -> None:
        view = memoryview(block)
        while view and not self.stop:
            sent = client.send(view)
            if sent == 0:
                raise ConnectionError("socket closed during block")
            if sent != len(view):
                self.metrics["partial_sends"] += 1
            view = view[sent:]

    def run(self, ready_file: Path | None = None) -> dict:
        payload = self.pcm.read_bytes()
        if len(payload) % self.block_bytes:
            raise ValueError(f"PCM length must be a multiple of {self.block_bytes} bytes")
        started = time.monotonic()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(1)
            server.settimeout(0.5)
            if ready_file:
                ready_file.parent.mkdir(parents=True, exist_ok=True)
                ready_file.write_text(json.dumps({"host": self.host, "port": self.port}) + "\n")
            offset = 0
            deadline = time.monotonic()
            while offset < len(payload) and not self.stop:
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                with client:
                    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    while offset < len(payload) and not self.stop:
                        block_start = time.monotonic()
                        block = payload[offset:offset + self.block_bytes]
                        try:
                            self.send_all(client, block)
                        except (BrokenPipeError, ConnectionError, ConnectionResetError):
                            self.metrics["reconnects"] += 1
                            break
                        offset += len(block)
                        self.metrics["blocks_sent"] += 1
                        self.metrics["bytes_sent"] += len(block)
                        elapsed_ms = (time.monotonic() - block_start) * 1000.0
                        self.metrics["processing_latency_ms_max"] = max(self.metrics["processing_latency_ms_max"], elapsed_ms)
                        if self.realtime:
                            deadline += self.block_seconds
                            delay = deadline - time.monotonic()
                            if delay > 0:
                                time.sleep(delay)
                            else:
                                self.metrics["pacing_deadline_misses"] += 1
        wall = time.monotonic() - started
        audio_seconds = self.metrics["blocks_sent"] * self.block_seconds
        self.metrics.update({"wall_seconds": wall, "audio_seconds": audio_seconds,
                             "realtime_factor": audio_seconds / wall if wall else None})
        return self.metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pcm", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10000)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--no-realtime", action="store_true")
    parser.add_argument("--metrics", type=Path)
    args = parser.parse_args()
    server = PCMServer(args.pcm, args.host, args.port, realtime=not args.no_realtime)
    signal.signal(signal.SIGINT, lambda *_: setattr(server, "stop", True))
    signal.signal(signal.SIGTERM, lambda *_: setattr(server, "stop", True))
    metrics = server.run(args.ready_file)
    if args.metrics:
        args.metrics.parent.mkdir(parents=True, exist_ok=True)
        args.metrics.write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
