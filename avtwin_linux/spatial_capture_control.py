from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable

from .spatial_calibration import read_avpc


@dataclass(frozen=True, slots=True)
class MapCaptureResult:
    command_id: str
    accepted: bool
    state: str
    reason: str
    output_path: str
    point_count: int | None = None
    duration_seconds: float | None = None
    log: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SpatialCaptureController:
    """Own the one-at-a-time ROS map capture and calibration HTTP process."""

    def __init__(
        self,
        project_dir: Path,
        *,
        output_path: Path | None = None,
        topic: str = "/cloud_registered_body",
        calibration_port: int = 5010,
        capture_command: Callable[[str, float], list[str]] | None = None,
    ):
        self.project_dir = project_dir.resolve()
        self.output_path = (output_path or (
            self.project_dir / "avtwin_linux" / "calibration" / "lidar_map.avpc"
        )).resolve()
        self.topic = topic
        self.calibration_port = int(calibration_port)
        self._capture_command = capture_command or self._default_capture_command
        self._lock = threading.RLock()
        self._capture_process: subprocess.Popen[str] | None = None
        self._capture_command_id: str | None = None
        self._results: dict[str, MapCaptureResult] = {}
        self._server_process: subprocess.Popen[str] | None = None

    def _default_capture_command(self, command_id: str, duration: float) -> list[str]:
        del command_id
        return [
            "/usr/bin/python3", "-m", "avtwin_linux.ros_map_capture",
            "--topic", self.topic,
            "--duration", f"{duration:g}",
            "--output", str(self.output_path),
        ]

    @property
    def is_capturing(self) -> bool:
        with self._lock:
            return self._capture_process is not None and self._capture_process.poll() is None

    def request_start(
        self,
        command_id: str,
        *,
        duration_seconds: float = 12.0,
        on_complete: Callable[[MapCaptureResult], None] | None = None,
    ) -> MapCaptureResult:
        command_id = str(command_id).strip()
        if not command_id:
            return MapCaptureResult("", False, "rejected", "missing_command_id", str(self.output_path))
        if not 3.0 <= float(duration_seconds) <= 60.0:
            return MapCaptureResult(
                command_id, False, "rejected", "duration_out_of_range_3_to_60_seconds",
                str(self.output_path),
            )
        with self._lock:
            cached = self._results.get(command_id)
            if cached is not None:
                return cached
            if self._capture_process is not None and self._capture_process.poll() is None:
                if self._capture_command_id == command_id:
                    return MapCaptureResult(
                        command_id, True, "capturing", "duplicate_request_reack",
                        str(self.output_path), duration_seconds=float(duration_seconds),
                    )
                return MapCaptureResult(
                    command_id, False, "busy", "another_map_capture_is_running",
                    str(self.output_path),
                )
            try:
                self.output_path.parent.mkdir(parents=True, exist_ok=True)
                process = subprocess.Popen(
                    self._capture_command(command_id, float(duration_seconds)),
                    cwd=self.project_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except OSError as exc:
                result = MapCaptureResult(
                    command_id, False, "failed", f"process_start_failed:{exc}",
                    str(self.output_path),
                )
                self._results[command_id] = result
                return result
            self._capture_process = process
            self._capture_command_id = command_id
        threading.Thread(
            target=self._wait_capture,
            args=(command_id, float(duration_seconds), process, on_complete),
            name="avtwin-lidar-map-capture",
            daemon=True,
        ).start()
        return MapCaptureResult(
            command_id, True, "capturing", "accepted_started",
            str(self.output_path), duration_seconds=float(duration_seconds),
        )

    def _wait_capture(
        self,
        command_id: str,
        duration_seconds: float,
        process: subprocess.Popen[str],
        on_complete: Callable[[MapCaptureResult], None] | None,
    ) -> None:
        output, _ = process.communicate()
        point_count: int | None = None
        if process.returncode == 0:
            try:
                points, _metadata = read_avpc(self.output_path)
                point_count = int(points.shape[0])
                if point_count < 80:
                    raise ValueError("雷达地图有效点不足 80")
                result = MapCaptureResult(
                    command_id, True, "completed", "capture_completed",
                    str(self.output_path), point_count, duration_seconds, output[-8000:],
                )
            except (OSError, ValueError) as exc:
                result = MapCaptureResult(
                    command_id, False, "failed", f"output_validation_failed:{exc}",
                    str(self.output_path), point_count, duration_seconds, output[-8000:],
                )
        else:
            result = MapCaptureResult(
                command_id, False, "failed", f"capture_process_exit_{process.returncode}",
                str(self.output_path), None, duration_seconds, output[-8000:],
            )
        if result.state == "completed":
            server_ok, server_reason = self.ensure_calibration_server()
            if not server_ok:
                result = MapCaptureResult(
                    result.command_id, result.accepted, result.state,
                    f"capture_completed_but_calibration_{server_reason}",
                    result.output_path, result.point_count, result.duration_seconds, result.log,
                )
        with self._lock:
            if self._capture_process is process:
                self._capture_process = None
                self._capture_command_id = None
            self._results[command_id] = result
            if len(self._results) > 128:
                self._results.pop(next(iter(self._results)))
        if on_complete is not None:
            on_complete(result)

    def request_stop(self) -> MapCaptureResult:
        with self._lock:
            process = self._capture_process
            command_id = self._capture_command_id or "local"
            if process is None or process.poll() is not None:
                return MapCaptureResult(
                    command_id, True, "idle", "already_stopped", str(self.output_path),
                )
            process.terminate()
        return MapCaptureResult(
            command_id, True, "stopping", "termination_requested", str(self.output_path),
        )

    def ensure_calibration_server(self) -> tuple[bool, str]:
        with self._lock:
            if self._server_process is not None and self._server_process.poll() is None:
                return True, "already_running"
            command = [
                sys.executable, "-m", "avtwin_linux.calibration_server",
                "--host", "0.0.0.0", "--port", str(self.calibration_port),
                "--lidar-map", str(self.output_path),
            ]
            try:
                process = subprocess.Popen(
                    command, cwd=self.project_dir,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError as exc:
                return False, f"server_start_failed:{exc}"
            self._server_process = process
        time.sleep(0.08)
        if process.poll() is not None:
            return False, f"server_exit_{process.returncode}"
        return True, "started"

    def close(self) -> None:
        with self._lock:
            processes = [self._capture_process, self._server_process]
            self._capture_process = None
            self._server_process = None
            self._capture_command_id = None
        for process in processes:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
