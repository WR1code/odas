"""ODAS 子进程和非阻塞 JSON 读取线程。"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, TextIO

from .json_stream import parse_stream


class ODASReader:
    def __init__(self, queue_size: int = 5) -> None:
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_size)
        self.stop_event = threading.Event()
        self.process: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self._owned_stream: TextIO | None = None

    def start_process(self, odas_bin: Path, config: Path) -> None:
        if not odas_bin.is_file() or not os.access(odas_bin, os.X_OK):
            raise FileNotFoundError(f"ODAS 不存在或不可执行：{odas_bin}")
        if not config.is_file():
            raise FileNotFoundError(f"配置文件不存在：{config}")
        command = ["stdbuf", "-oL", str(odas_bin), "-c", str(config)]
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        assert self.process.stdout is not None
        self._start_thread(self.process.stdout)

    def start_stream(self, stream: TextIO, owned: bool = False) -> None:
        self._owned_stream = stream if owned else None
        self._start_thread(stream)

    def _start_thread(self, stream: TextIO) -> None:
        self.thread = threading.Thread(target=self._read, args=(stream,), name="odas-json-reader", daemon=True)
        self.thread.start()

    def _read(self, stream: TextIO) -> None:
        try:
            for message in parse_stream(stream):
                if self.stop_event.is_set():
                    break
                self._put_latest(message)
        except (OSError, UnicodeError) as exc:
            if not self.stop_event.is_set():
                self.error = f"读取 ODAS 输出失败：{exc}"
        finally:
            if self._owned_stream is not None:
                self._owned_stream.close()

    def _put_latest(self, message: dict[str, Any]) -> None:
        while True:
            try:
                self.messages.put_nowait(message)
                return
            except queue.Full:
                try:
                    self.messages.get_nowait()
                except queue.Empty:
                    return

    def latest(self) -> dict[str, Any] | None:
        newest = None
        while True:
            try:
                newest = self.messages.get_nowait()
            except queue.Empty:
                return newest

    def stop(self) -> None:
        self.stop_event.set()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2.0)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2.0)
        if self._owned_stream is not None and not self._owned_stream.closed:
            self._owned_stream.close()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
